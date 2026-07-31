import pandas as pd
import os
import io
import re
import json
import tempfile
import threading
from flask import Flask, redirect, render_template, request

app = Flask(__name__)

USUARIOS = ["Debbie Velazquez","Deyron Garcia","Luis Sierra","Jennifer Salgado","Daniel Salinas","Norwin Gonzalez","Isaac Zelaya","Cesar Flores"]
MESES = ["Enero","Febrero","Marzo","Abril","Mayo","Junio","Julio","Agosto","Septiembre","Octubre","Noviembre","Diciembre"]

# --- Persistencia en disco -------------------------------------------------
# almacen vivia solo en memoria RAM: si el servidor corre con varios workers
# (p.ej. gunicorn) o se reinicia, los datos guardados con /hp, /add_comp,
# /del_comp, etc. desaparecen sin aviso. Ahora se guarda en un archivo JSON
# compartido en disco despues de cada cambio, y se recarga al iniciar.
ALMACEN_FILE = os.environ.get("ALMACEN_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "almacen.json"))
_lock = threading.Lock()

def _key_to_str(k):
    return f"{k[0]}||{k[1]}"

def _str_to_key(s):
    mes, usuario = s.split("||", 1)
    return (mes, usuario)

def cargar_almacen():
    if not os.path.exists(ALMACEN_FILE):
        return {}
    try:
        with open(ALMACEN_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {_str_to_key(k): v for k, v in raw.items()}
    except Exception:
        return {}

def guardar_almacen():
    """Escritura atomica: escribe a un archivo temporal y luego renombra,
    para evitar dejar el archivo corrupto si dos procesos escriben a la vez."""
    with _lock:
        raw = {_key_to_str(k): v for k, v in almacen.items()}
        dir_ = os.path.dirname(ALMACEN_FILE) or "."
        fd, tmp_path = tempfile.mkstemp(dir=dir_, prefix=".almacen_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False)
            os.replace(tmp_path, ALMACEN_FILE)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

almacen = cargar_almacen()  # (mes, usuario) -> info_dict

def detect_sep(file_bytes):
    sample = file_bytes[:4096].decode("utf-8-sig", errors="ignore")
    return ";" if sample.count(";") > sample.count(",") else ","

def calc_hours_per_row(df):
    cols_lower = {c.lower(): c for c in df.columns}
    if "hora" in cols_lower and "minuto" in cols_lower:
        h = pd.to_numeric(df[cols_lower["hora"]], errors="coerce").fillna(0)
        m = pd.to_numeric(df[cols_lower["minuto"]], errors="coerce").fillna(0)
        return h + m / 60
    return None

def calc_total(df):
    h = calc_hours_per_row(df)
    return pd.Timedelta(hours=h.sum()) if h is not None else None

def filter_by_project_code(df):
    cols_lower = {c.lower(): c for c in df.columns}
    col = cols_lower.get("codigoproyecto")
    if col is None:
        return df, None
    s = df[col].astype(str).str.strip()
    nums = pd.to_numeric(s, errors="coerce")
    mask = (~nums.notna()) | (nums.isin([2, 3]))
    return df[mask].copy(), None

def group_dep(df):
    hours = calc_hours_per_row(df)
    if hours is None: return None
    cols_lower = {c.lower(): c for c in df.columns}
    cols = []
    if "usuario" in cols_lower: cols.append(cols_lower["usuario"])
    if "fechawork" in cols_lower:
        df = df.copy()
        df["_Mes"] = pd.to_datetime(df[cols_lower["fechawork"]], errors="coerce", dayfirst=True).dt.month.map(lambda m: MESES[int(m)-1] if pd.notna(m) and 1 <= m <= 12 else "S/F")
        cols.append("_Mes")
    if "codigoproyecto" in cols_lower: cols.append(cols_lower["codigoproyecto"])
    if "descripcion" in cols_lower: cols.append(cols_lower["descripcion"])
    if not cols: return None
    grupo = df[cols].copy(); grupo["horas_float"] = hours
    agg = grupo.groupby(cols, as_index=False)["horas_float"].sum()
    return agg.to_html(classes="table table-sm small-table", index=False)

def fmt_hours(td):
    if td is None: return "0h 00m"
    total_h = td.total_seconds() / 3600
    h = int(total_h); m = int(round((total_h - h) * 60))
    return f"{h}h {m:02d}m"

def analizar(df):
    df_dep, _ = filter_by_project_code(df)
    th = calc_total(df)
    comp_f = th.total_seconds() / 3600 - 176 if th else 0
    dep_th = calc_total(df_dep) if len(df_dep) > 0 else None
    dep_f = dep_th.total_seconds() / 3600 if dep_th else 0
    dep_r = dep_f - 176
    condicion = th.total_seconds() / 3600 > 176 if th else False
    condicion_dep = dep_f > 176 if dep_th else False
    total_h = th.total_seconds() / 3600 if th else 0
    return {
        "total": fmt_hours(th),
        "total_h": total_h,
        "condicion": condicion,
        "condicion_dep": condicion_dep,
        "comp": fmt_hours(pd.Timedelta(hours=comp_f)) if condicion else "0h 00m",
        "comp_h": comp_f if condicion else 0,
        "dep_total": fmt_hours(dep_th) if dep_th is not None else "0h 00m",
        "dep_total_h": dep_f,
        "dep_resta": fmt_hours(pd.Timedelta(hours=dep_r)) if condicion_dep else "0h 00m",
        "dep_resta_h": dep_r if condicion_dep else 0,
        "dep_pct": f"{(dep_f / 176) * 100:.1f}%" if condicion_dep else "0%",
        "total_html": group_dep(df) if len(df) > 0 else None,
        "dep_html": group_dep(df_dep) if len(df_dep) > 0 else None,
        "rows": len(df),
        "duplicadas": int(df.duplicated().sum()),
        "hp": 0,
        "compensadas": [],  # lista de {"cant": float, "fecha": str}
    }

@app.route("/reset/<mes>", methods=["POST"])
def reset_mes(mes):
    global almacen
    almacen = cargar_almacen()
    almacen = {k:v for k,v in almacen.items() if k[0] != mes}
    guardar_almacen()
    return redirect("/")

def recalcular_dep_resta(e):
    """Recalcula dep_resta segun hp y compensadas"""
    total_comp = sum(c["cant"] for c in e.get("compensadas", []))
    hp = e.get("hp", 0)
    dep_r = e["dep_total_h"] - 176 - hp - total_comp
    if dep_r > 0:
        e["dep_resta"] = fmt_hours(pd.Timedelta(hours=dep_r))
        e["dep_resta_h"] = dep_r
    else:
        e["dep_resta"] = "0h 00m"
        e["dep_resta_h"] = 0

@app.route("/hp", methods=["POST"])
def set_hp():
    global almacen
    mes = request.form.get("mes", "")
    usuario = request.form.get("usuario", "")
    try: hp = float(request.form.get("hp", 0))
    except: hp = 0
    almacen = cargar_almacen()
    key = (mes, usuario)
    if key in almacen:
        almacen[key]["hp"] = hp
        recalcular_dep_resta(almacen[key])
        guardar_almacen()
    return redirect("/")

@app.route("/add_comp", methods=["POST"])
def add_comp():
    global almacen
    mes = request.form.get("mes", "")
    usuario = request.form.get("usuario", "")
    try: cant = float(request.form.get("cant", 0))
    except: cant = 0
    fecha = request.form.get("fecha", "")
    almacen = cargar_almacen()
    key = (mes, usuario)
    if key in almacen and cant > 0:
        almacen[key].setdefault("compensadas", []).append({"cant": cant, "fecha": fecha})
        recalcular_dep_resta(almacen[key])
        guardar_almacen()
    return redirect("/")

@app.route("/del_comp", methods=["POST"])
def del_comp():
    global almacen
    mes = request.form.get("mes", "")
    usuario = request.form.get("usuario", "")
    try: idx = int(request.form.get("idx", -1))
    except: idx = -1
    almacen = cargar_almacen()
    key = (mes, usuario)
    if key in almacen and 0 <= idx < len(almacen[key].get("compensadas", [])):
        almacen[key]["compensadas"].pop(idx)
        recalcular_dep_resta(almacen[key])
        guardar_almacen()
    return redirect("/")

@app.route("/", methods=["GET", "POST"])
def index():
    global almacen
    almacen = cargar_almacen()
    info = None
    error = None

    if request.method == "POST":
        usuario = request.form.get("usuario", "")
        mes_texto = request.form.get("mes", "")
        file = request.files.get("file")
        if not file or file.filename == "":
            error = "Selecciona un archivo"
        else:
            try:
                raw = file.read()
                ext = os.path.splitext(file.filename)[1].lower()
                if ext in (".xlsx", ".xls"):
                    buf = io.BytesIO(raw)
                    try: df = pd.read_excel(buf, engine="openpyxl" if ext == ".xlsx" else "xlrd")
                    except Exception: buf.seek(0); tables = pd.read_html(buf); df = tables[0] if tables else None
                elif ext == ".csv":
                    sep = detect_sep(raw); df = pd.read_csv(io.BytesIO(raw), encoding="utf-8", sep=sep)
                else: error = "Formato no soportado"; df = None
            except Exception as e: error = f"Error al leer: {e}"; df = None

            if df is not None:
                df.columns = df.columns.str.strip().str.replace("\ufeff", "", regex=False)
                cols_lower = {c.lower(): c for c in df.columns}
                info = analizar(df)
                info["nombre"] = usuario
                info["mes"] = mes_texto
                info["archivo"] = file.filename
                if info["rows"] == 0:
                    error = f"No se encontraron registros de {usuario} en {mes_texto}"
                else:
                    almacen[(mes_texto, usuario)] = info
                    guardar_almacen()

    info_idx_mes = MESES.index(info["mes"]) + 1 if info and info["mes"] in MESES else None
    info_idx_u = USUARIOS.index(info["nombre"]) + 1 if info and info["nombre"] in USUARIOS else None

    # Totales por mes (departamento) — suma directa de cada campo
    totales_mes = {}
    for m in MESES:
        entries = [v for k,v in almacen.items() if k[0] == m]
        if not entries:
            continue
        sum_total = sum(e["total_h"] for e in entries)
        sum_comp = sum(e["comp_h"] for e in entries)
        sum_dep = sum(e["dep_total_h"] for e in entries)
        sum_dep_r = sum(e["dep_resta_h"] for e in entries)
        sum_hp = sum(e.get("hp", 0) for e in entries)
        sum_compensadas = sum(sum(c["cant"] for c in e.get("compensadas", [])) for e in entries)
        sum_dep_netas = sum_dep - sum_hp - sum_compensadas
        num = len(entries)
        dep_pct_val = (sum_dep / (176 * num)) * 100 if sum_dep > 0 else 0
        totales_mes[m] = {
            "total": fmt_hours(pd.Timedelta(hours=sum_total)),
            "comp": fmt_hours(pd.Timedelta(hours=sum_comp)) if sum_comp > 0 else "0h 00m",
            "dep_total": fmt_hours(pd.Timedelta(hours=sum_dep)),
            "dep_netas": fmt_hours(pd.Timedelta(hours=sum_dep_netas)) if sum_dep_netas > 0 else "0h 00m",
            "dep_resta": fmt_hours(pd.Timedelta(hours=sum_dep_r)) if sum_dep_r > 0 else "0h 00m",
            "total_compensado": sum_compensadas,
            "dep_pct": f"{dep_pct_val:.1f}%" if dep_pct_val > 0 else "0%",
            "usuarios_subidos": num,
            "condicion": sum_comp > 0,
            "condicion_dep": sum_dep_r > 0,
        }

    # Gran total (suma de todos los meses)
    gt_total = sum(t["total_h"] for t in almacen.values())
    gt_comp = sum(t["comp_h"] for t in almacen.values())
    gt_dep = sum(t["dep_total_h"] for t in almacen.values())
    gt_hp = sum(t.get("hp", 0) for t in almacen.values())
    gt_compensadas = sum(sum(c["cant"] for c in t.get("compensadas", [])) for t in almacen.values())
    gt_dep_netas = gt_dep - gt_hp - gt_compensadas
    gt_dep_r = sum(t["dep_resta_h"] for t in almacen.values())
    gt_pct = (gt_dep / (176 * len(almacen))) * 100 if gt_dep > 0 and almacen else 0

    # Totales por usuario (todos los meses)
    totales_usuario = {}
    for u in USUARIOS:
        u_entries = [v for k,v in almacen.items() if k[1] == u]
        if u_entries:
            sum_h = sum(e["total_h"] for e in u_entries)
            sum_dep = sum(e["dep_total_h"] for e in u_entries)
            sum_hp = sum(e.get("hp", 0) for e in u_entries)
            sum_compensadas = sum(sum(c["cant"] for c in e.get("compensadas", [])) for e in u_entries)
            totales_usuario[u] = {"total": fmt_hours(pd.Timedelta(hours=sum_h)), "total_h": sum_h,
                                  "dep_total": fmt_hours(pd.Timedelta(hours=sum_dep)),
                                  "dep_netas": fmt_hours(pd.Timedelta(hours=sum_dep - sum_hp - sum_compensadas))}

    return render_template("index.html", meses=MESES, usuarios=USUARIOS, almacen=almacen, totales_mes=totales_mes,
                           totales_usuario=totales_usuario,
                           gt={"total": fmt_hours(pd.Timedelta(hours=gt_total)),
                               "comp": fmt_hours(pd.Timedelta(hours=gt_comp)) if gt_comp > 0 else "0h 00m",
                               "dep": fmt_hours(pd.Timedelta(hours=gt_dep)),
                               "dep_netas": fmt_hours(pd.Timedelta(hours=gt_dep_netas)) if gt_dep_netas > 0 else "0h 00m",
                               "dep_r": fmt_hours(pd.Timedelta(hours=gt_dep_r)) if gt_dep_r > 0 else "0h 00m",
                               "pct": f"{gt_pct:.1f}%" if gt_pct > 0 else "0%"},
                           info=info, info_idx_mes=info_idx_mes, info_idx_u=info_idx_u, error=error)

if __name__ == "__main__":
    import os, socket
    port = int(os.environ.get("PORT", 5000))
    hostname = socket.gethostname()
    ip = socket.gethostbyname(hostname)
    print(f"\nLocal:  http://localhost:{port}")
    print(f"Red:    http://{ip}:{port}\n")
    app.run(debug=True, port=port, host="0.0.0.0")
