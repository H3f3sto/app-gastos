import base64
import hashlib
import io
import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo
import pandas as pd
import PIL.Image
import pypdfium2 as pdfium
import streamlit as st
from google import genai
from google.genai import errors as genai_errors
from dotenv import load_dotenv

# Cargar variables (.env)
load_dotenv()

import db
from auth import get_cookie_manager, obtener_user_id, introducir_codigo_manual

ZONA_MADRID = ZoneInfo("Europe/Madrid")

MAX_REINTENTOS = 3
ESPERA_BASE_SEGUNDOS = {503: 4, 429: 15}


def calcular_hash_archivo(uploaded_file) -> str:
    """Calcula la firma SHA-256 única para cualquier tipo de archivo (JPG, PNG, PDF)."""
    uploaded_file.seek(0)
    contenido = uploaded_file.read()
    uploaded_file.seek(0)
    return hashlib.sha256(contenido).hexdigest()


def convertir_a_imagen_pil(uploaded_file) -> PIL.Image.Image:
    """
    Convierte cualquier archivo subido (incluidos PDF de Amazon/facturas)
    a un objeto PIL Image optimizado y listo para Gemini.
    """
    uploaded_file.seek(0)
    nombre_archivo = uploaded_file.name.lower()

    if nombre_archivo.endswith(".pdf"):
        # Convertir la primera página del PDF a imagen en memoria
        pdf = pdfium.PdfDocument(uploaded_file.read())
        uploaded_file.seek(0)
        page = pdf[0]
        bitmap = page.render(scale=2)  # Escala x2 para alta nitidez en textos pequeños
        pil_image = bitmap.to_pil()
    else:
        pil_image = PIL.Image.open(uploaded_file)

    if pil_image.mode in ("RGBA", "P"):
        pil_image = pil_image.convert("RGB")

    # Redimensionado inteligente para velocidad y ahorro de red
    max_ancho = 1024
    if pil_image.width > max_ancho:
        proporcion = max_ancho / float(pil_image.width)
        alto_nuevo = int((float(pil_image.height) * float(proporcion)))
        pil_image = pil_image.resize((max_ancho, alto_nuevo), PIL.Image.Resampling.LANCZOS)

    buffer = io.BytesIO()
    pil_image.save(buffer, format="JPEG", quality=80, optimize=True)
    buffer.seek(0)
    return PIL.Image.open(buffer)


def analizar_ticket_con_reintento(chat, prompt, imagen, status_placeholder):
    """Envía la petición a Gemini aplicando backoff exponencial si hay sobrecarga."""
    ultimo_error = None
    for intento in range(1, MAX_REINTENTOS + 1):
        try:
            return chat.send_message([prompt, imagen])
        except (genai_errors.ServerError, genai_errors.ClientError) as e:
            ultimo_error = e
            codigo = getattr(e, "code", None)
            if codigo not in ESPERA_BASE_SEGUNDOS or intento == MAX_REINTENTOS:
                raise
            espera = ESPERA_BASE_SEGUNDOS[codigo] * (2 ** (intento - 1))
            motivo = (
                "el límite de frecuencia de Gemini"
                if codigo == 429
                else "Gemini está saturado ahora mismo"
            )
            status_placeholder.warning(
                f"Alcanzado {motivo}. Reintentando en {espera}s "
                f"(intento {intento}/{MAX_REINTENTOS})..."
            )
            time.sleep(espera)
    raise ultimo_error


# Configuración del diseño
st.set_page_config(
    page_title="Scanner de Tickets - MVP", page_icon="🧾", layout="wide"
)

# Identificación del usuario y cliente
cookies = get_cookie_manager()
user_id = obtener_user_id(cookies)
ip_cliente = st.context.ip_address

# Inicializar cliente de Gemini
API_KEY = os.environ.get("GEMINI_API_KEY")
if not API_KEY:
    st.error("Falta la variable de entorno GEMINI_API_KEY en tu archivo .env. Configúrala antes de continuar.")
    st.stop()
client = genai.Client(api_key=API_KEY)

# Prompt adaptado tanto para tickets de tienda física como facturas/recibos digitales (Amazon, etc.)
PROMPT = """
Analiza la imagen adjunta. Puede ser un ticket de compra físico o un recibo/factura digital (como Amazon, correo electrónico o e-commerce).
Devuelve UNICAMENTE un JSON strictly válido con esta estructura exacta, sin bloques de código Markdown ni texto explicativo:
{
  "metadatos": {
    "tienda": "",
    "nif_cif": "",
    "fecha": ""
  },
  "items": [
    {
      "concepto_raw": "",
      "cantidad": 1.0,
      "precio_unitario": 0.0,
      "precio_total": 0.0,
      "gemini_original": "",
      "accion_usuario": "NATIVO",
      "valor_final": ""
    }
  ],
  "resumen_financiero": {
    "total": 0.0
  }
}
Instrucciones adicionales:
- Si es un recibo de Amazon o e-commerce, usa "Amazon" o el nombre del vendedor en "tienda".
- Si no hay fecha visible, coloca la fecha actual o déjala en blanco "".
- Si no puedes distinguir ítems individuales, registra el concepto global en un solo ítem.
"""

# Inicialización de estados de sesión
if "ticket_data" not in st.session_state:
    st.session_state["ticket_data"] = None

if "hashes_analizados" not in st.session_state:
    st.session_state["hashes_analizados"] = set()

if "guardando_ticket" not in st.session_state:
    st.session_state["guardando_ticket"] = False

# Encabezado principal
st.title("🧾 Procesador Inteligente de Tickets y Recibos")
st.caption("MVP · Gemini 3.6 Flash · Historial de gastos personal")

# Métricas en el panel lateral (Sidebar)
restantes = db.tickets_restantes(user_id)
st.sidebar.metric("Tickets disponibles este mes", restantes)
st.sidebar.caption("🔑 Tu código de cuenta personal:")
st.sidebar.code(user_id, language=None)
st.sidebar.caption("💡 Guarda este código. Si borras las cookies o cambias de dispositivo, puedes ingresarlo aquí abajo para recuperar tu historial completo:")
introducir_codigo_manual(cookies)


# ─── FUNCIÓN AUXILIAR DE PROCESAMIENTO ─────────────────────────────────────────
def ejecutar_analisis_ticket(uploaded_file, hash_actual, status_placeholder):
    """Procesa el documento (JPG, PNG, PDF) con Gemini."""
    try:
        imagen_optimizada = convertir_a_imagen_pil(uploaded_file)
        chat = client.chats.create(model="models/gemini-3.6-flash")
        response = analizar_ticket_con_reintento(
            chat, PROMPT, imagen_optimizada, status_placeholder
        )
        
        # Descontar uso e incrementar contadores
        db.incrementar_contador(user_id)
        db.registrar_analisis_ip(ip_cliente)
        st.session_state["hashes_analizados"].add(hash_actual)
        
        # Parsear JSON de Gemini
        texto = response.text.replace("```json", "").replace("```", "").strip()
        datos = json.loads(texto)
        st.session_state["ticket_data"] = datos
        status_placeholder.empty()
        st.success("¡Documento procesado correctamente!")
        st.rerun()
    except genai_errors.ClientError as e:
        status_placeholder.empty()
        if getattr(e, "code", None) == 429:
            db.registrar_evento("analisis_error_429", user_id)
            st.error("Límite de frecuencia de Gemini alcanzado. Espera un momento.")
        else:
            db.registrar_evento("analisis_error_otro", user_id)
            st.error(f"Error al procesar el documento: {str(e)}")
    except genai_errors.ServerError:
        status_placeholder.empty()
        db.registrar_evento("analisis_error_503", user_id)
        st.error("Gemini está saturado. Inténtalo de nuevo en unos minutos.")
    except Exception as e:
        status_placeholder.empty()
        db.registrar_evento("analisis_error_otro", user_id)
        st.error(f"Error al procesar el documento: {str(e)}")


# ─── VENTANA DE DIÁLOGO: ADVERTENCIA DE DUPLICADOS EN HISTORIAL ───────────────
@st.dialog("⚠️ Documento ya registrado en tu historial")
def mostrar_dialogo_duplicado(uploaded_file, hash_actual):
    st.write(
        "Detectamos que esta imagen o PDF **ya se encuentra guardado en tu historial de gastos**."
    )
    st.warning(
        "Si decides analizarlo de nuevo, consumirás **1 intento** de tu cuota mensual."
    )
    st.write("¿Realmente deseas volver a analizarlo?")

    col_si, col_no = st.columns(2)
    with col_si:
        if st.button("SÍ, analizar de nuevo", type="primary"):
            status_placeholder = st.empty()
            with st.spinner("⚡ Procesando documento..."):
                ejecutar_analisis_ticket(uploaded_file, hash_actual, status_placeholder)
    with col_no:
        if st.button("NO, cancelar"):
            st.rerun()


# Pestañas principales
tab_scanner, tab_historial, tab_resumen = st.tabs([
    "📷 Escanear Documento",
    "📋 Historial",
    "📊 Resumen Mensual"
])

# ─── TAB 1: SCANNER ───────────────────────────────────────────────────────────
with tab_scanner:
    col_left, col_right = st.columns([1, 1])

    with col_left:
        st.subheader("1. Cargar Ticket o Recibo")

        limite_usuario_ok = db.puede_escanear(user_id)
        limite_ip_ok = db.puede_ip_analizar(ip_cliente)
        puede_analizar = limite_usuario_ok and limite_ip_ok

        if not limite_usuario_ok:
            st.warning(
                f"Has alcanzado el límite de {db.LIMITE_TICKETS_FREE} tickets este mes. "
                "El contador se reiniciará el próximo mes."
            )
        elif not limite_ip_ok:
            st.warning(
                "Se han hecho demasiados análisis desde tu conexión en la última hora. "
                "Inténtalo de nuevo más tarde."
            )

        uploaded_file = st.file_uploader(
            "Selecciona o arrastra una imagen (JPG, PNG) o recibo PDF (Amazon, etc.)",
            type=["jpg", "jpeg", "png", "pdf"],
            disabled=not puede_analizar,
        )

        if uploaded_file is not None:
            if uploaded_file.name.lower().endswith(".pdf"):
                st.info(f"📄 Archivo PDF cargado: **{uploaded_file.name}**")
            else:
                st.image(uploaded_file, caption="Vista previa del documento", width=400)
            
            # Calcular hash único
            hash_actual = calcular_hash_archivo(uploaded_file)
            st.session_state["hash_imagen_actual"] = hash_actual

            # Control de duplicados
            ya_en_sesion = hash_actual in st.session_state["hashes_analizados"]
            ya_en_historial_db = db.ticket_existe_en_historial(user_id, hash_actual)
            es_duplicado = ya_en_sesion or ya_en_historial_db

            if st.button("🔍 Analizar con Gemini", type="primary", disabled=not puede_analizar):
                if es_duplicado:
                    mostrar_dialogo_duplicado(uploaded_file, hash_actual)
                else:
                    status_placeholder = st.empty()
                    with st.spinner("⚡ Optimizando documento y analizando..."):
                        ejecutar_analisis_ticket(uploaded_file, hash_actual, status_placeholder)

    with col_right:
        st.subheader("2. Resultados y Auditoría")

        if st.session_state["ticket_data"] is not None:
            data = st.session_state["ticket_data"]
            meta = data.get("metadatos", {})

            st.markdown(f"**Establecimiento:** {meta.get('tienda', 'N/A')}")
            st.markdown(f"**CIF/NIF:** {meta.get('nif_cif', 'N/A')}")
            st.markdown(f"**Fecha:** {meta.get('fecha', 'N/A')}")
            st.divider()

            items = data.get("items", [])
            if items:
                df = pd.DataFrame(items)
                expected_cols = [
                    "concepto_raw", "cantidad", "precio_unitario",
                    "precio_total", "gemini_original", "accion_usuario", "valor_final"
                ]
                df = df[[c for c in expected_cols if c in df.columns]]
                st.write("**Productos detectados (editable):**")
                edited_df = st.data_editor(
                    df, num_rows="dynamic",
                    use_container_width=True, key="editor_items"
                )
                items = edited_df.to_dict(orient="records")

            resumen = data.get("resumen_financiero", {})
            total = resumen.get("total", 0.0)
            st.metric(label="Total Calculado", value=f"{total:.2f} €")

            st.divider()
            
            firma_ticket = hashlib.sha1(
                json.dumps({"meta": meta, "total": total, "items": items}, sort_keys=True, default=str).encode()
            ).hexdigest()
            
            ya_guardado = st.session_state.get("ultimo_guardado_firma") == firma_ticket

            # BOTÓN CORREGIDO: Guarda directamente sin colisión de callbacks ni congelamientos
            if st.button(
                "💾 Guardar en historial", 
                type="primary", 
                disabled=ya_guardado or st.session_state["guardando_ticket"]
            ):
                st.session_state["guardando_ticket"] = True
                
                entrada = {
                    "fecha_registro": datetime.now(ZONA_MADRID).strftime("%Y-%m-%d %H:%M"),
                    "tienda": meta.get("tienda", "Desconocida"),
                    "fecha_ticket": meta.get("fecha", ""),
                    "nif_cif": meta.get("nif_cif", ""),
                    "total": total,
                    "num_productos": len(items),
                    "items": items,
                    "hash_imagen": st.session_state.get("hash_imagen_actual")
                }
                
                with st.spinner("Guardando en la base de datos..."):
                    exito = db.guardar_ticket(user_id, entrada)
                
                st.session_state["guardando_ticket"] = False
                
                if exito:
                    st.session_state["ultimo_guardado_firma"] = firma_ticket
                    st.session_state["ticket_data"] = None
                    st.success(f"✅ Ticket de {meta.get('tienda', 'tienda')} guardado correctamente.")
                    st.rerun()
                else:
                    st.warning("Este ticket ya se encuentra registrado en la base de datos.")
        else:
            st.info("Sube una imagen o PDF y pulsa 'Analizar con Gemini' para ver los resultados.")

# Obtener historial de Firestore
historial = db.obtener_historial(user_id)

# ─── TAB 2: HISTORIAL ─────────────────────────────────────────────────────────
with tab_historial:
    st.subheader("📋 Tickets guardados")

    if not historial:
        st.info("Aún no has guardado ningún ticket. Escanea uno y pulsa 'Guardar en historial'.")
    else:
        for entrada in historial:
            with st.expander(f"🧾 {entrada['tienda']} — {entrada['fecha_ticket']} — {entrada['total']:.2f} €"):
                st.markdown(f"**Registrado:** {entrada['fecha_registro']}")
                st.markdown(f"**CIF/NIF:** {entrada['nif_cif']}")
                st.markdown(f"**Productos:** {entrada['num_productos']}")
                if entrada.get("items"):
                    df_items = pd.DataFrame(entrada["items"])
                    cols = ["concepto_raw", "cantidad", "precio_unitario", "precio_total", "valor_final"]
                    df_items = df_items[[c for c in cols if c in df_items.columns]]
                    st.dataframe(df_items, use_container_width=True)

                st.divider()
                if st.button("🗑️ Borrar este ticket", key=f"borrar_{entrada['_doc_id']}"):
                    if db.borrar_ticket(user_id, entrada["_doc_id"]):
                        st.success("Ticket borrado.")
                        st.rerun()
                    else:
                        st.error("No se ha podido borrar el ticket.")

        st.divider()
        if st.button("⬇️ Exportar historial a CSV"):
            filas = []
            for entrada in historial:
                for item in entrada.get("items", []):
                    filas.append({
                        "fecha_registro": entrada["fecha_registro"],
                        "tienda": entrada["tienda"],
                        "fecha_ticket": entrada["fecha_ticket"],
                        "total_ticket": entrada["total"],
                        "producto": item.get("valor_final", item.get("concepto_raw", "")),
                        "cantidad": item.get("cantidad", ""),
                        "precio_unitario": item.get("precio_unitario", ""),
                        "precio_total": item.get("precio_total", "")
                    })
            df_export = pd.DataFrame(filas)
            csv = df_export.to_csv(index=False).encode("utf-8")
            st.download_button(
                label="📥 Descargar CSV",
                data=csv,
                file_name=f"historial_gastos_{datetime.now(ZONA_MADRID).strftime('%Y%m')}.csv",
                mime="text/csv"
            )

# ─── TAB 3: RESUMEN MENSUAL ───────────────────────────────────────────────────
with tab_resumen:
    st.subheader("📊 Resumen de gastos")

    if not historial:
        st.info("Guarda al menos un ticket para ver tu resumen de gastos.")
    else:
        df_resumen = pd.DataFrame([{
            "tienda": e["tienda"],
            "fecha": e["fecha_ticket"],
            "total": e["total"],
            "productos": e["num_productos"]
        } for e in historial])

        col1, col2, col3 = st.columns(3)
        col1.metric("Total gastado", f"{df_resumen['total'].sum():.2f} €")
        col2.metric("Tickets guardados", len(df_resumen))
        col3.metric("Gasto medio por ticket", f"{df_resumen['total'].mean():.2f} €")

        st.divider()
        st.write("**Gasto por establecimiento:**")
        df_por_tienda = df_resumen.groupby("tienda")["total"].sum().reset_index()
        df_por_tienda.columns = ["Tienda", "Total (€)"]
        df_por_tienda = df_por_tienda.sort_values("Total (€)", ascending=False)
        st.dataframe(df_por_tienda, use_container_width=True)

        st.divider()
        st.write("**Últimos tickets:**")
        st.dataframe(df_resumen.sort_values("fecha", ascending=False), use_container_width=True)

# ─── PANEL ADMINISTRADOR ──────────────────────────────────────────────────────
ADMIN_KEY = os.environ.get("ADMIN_KEY")
if ADMIN_KEY and st.query_params.get("clave") == ADMIN_KEY:
    st.divider()
    with st.expander("📊 Panel de métricas (solo admin)", expanded=True):
        metricas = db.obtener_metricas()
        
        c1, c2, c3 = st.columns(3)
        c1.metric("Usuarios totales", metricas["total_usuarios"])
        c2.metric("Activados", metricas["activados"], help="Analizaron al menos un ticket")
        c3.metric("Análisis con éxito", metricas["total_analisis_ok"])

        c4, c5, c6, c7 = st.columns(4)
        c4.metric("Volvieron otro día", metricas["volvieron_otro_dia"])
        c5.metric("Retención (% de activados)", f"{metricas['retencion_pct']}%")
        c6.metric("Días activos / usuario", metricas["dias_activos_promedio"])
        c7.metric("% análisis que se guardan", f"{metricas['tasa_guardado_pct']}%")
