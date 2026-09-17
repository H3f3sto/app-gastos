import base64
import json
import os
import time
from datetime import datetime
import pandas as pd
import streamlit as st
from google import genai
from google.genai import errors as genai_errors
from dotenv import load_dotenv

# Debe ejecutarse ANTES de importar auth (que lee COOKIE_PASSWORD del entorno
# en tiempo de importación) — si no, el .env nunca llega a aplicarse.
load_dotenv()

import db
from auth import get_cookie_manager, obtener_user_id, introducir_codigo_manual

MAX_REINTENTOS = 3
# Espera base en segundos por código de error, antes de duplicarse en cada intento.
# 503 (saturación temporal): ventana corta, 4s/8s/16s basta.
# 429 (límite de frecuencia compartido, RPM): la ventana es de 1 minuto completo,
# así que hace falta esperar más para que el cupo del minuto se libere.
ESPERA_BASE_SEGUNDOS = {503: 4, 429: 15}


def analizar_ticket_con_reintento(chat, prompt, imagen, status_placeholder):
    """Llama a Gemini con reintentos y backoff exponencial ante:
    - 503 ServerError: Gemini saturado temporalmente.
    - 429 ClientError: límite de frecuencia (RPM/RPD) superado, compartido
      entre todos los usuarios de la app.
    Otros errores se propagan de inmediato."""
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
                "el límite de frecuencia compartido de Gemini"
                if codigo == 429
                else "Gemini está saturado ahora mismo"
            )
            status_placeholder.warning(
                f"Alcanzado {motivo}. Reintentando en {espera}s "
                f"(intento {intento}/{MAX_REINTENTOS})..."
            )
            time.sleep(espera)
    raise ultimo_error

# Configuración de la página
st.set_page_config(
    page_title="Scanner de Tickets - MVP", page_icon="🧾", layout="wide"
)

# ─── Identificación de usuario (cookie + UUID) ────────────────────────────────
cookies = get_cookie_manager()
user_id = obtener_user_id(cookies)
ip_cliente = st.context.ip_address

# API Key de Gemini (obligatoria por variable de entorno, sin valor por defecto)
API_KEY = os.environ.get("GEMINI_API_KEY")
if not API_KEY:
    st.error("Falta la variable de entorno GEMINI_API_KEY. Configúrala antes de continuar.")
    st.stop()
client = genai.Client(api_key=API_KEY)

# Prompt
PROMPT = """
Analiza la imagen adjunta de este ticket de compra.
Devuelve un JSON estrictamente válido con esta estructura exacta, sin texto adicional:
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
"""

# Solo el ticket_data en curso vive en session_state; el historial viene de TinyDB
if "ticket_data" not in st.session_state:
    st.session_state["ticket_data"] = None

# Título
st.title("🧾 Procesador Inteligente de Tickets")
st.caption("MVP · Gemini 2.5 Flash · Historial de gastos personal")

# Estado del plan freemium, visible siempre
restantes = db.tickets_restantes(user_id)
st.sidebar.metric("Tickets disponibles este mes", restantes)
st.sidebar.caption("Tu código de usuario (guárdalo para recuperar tu historial):")
st.sidebar.code(user_id, language=None)
introducir_codigo_manual(cookies)

# Tabs principales
tab_scanner, tab_historial, tab_resumen = st.tabs([
    "📷 Escanear Ticket",
    "📋 Historial",
    "📊 Resumen Mensual"
])

# ─── TAB 1: SCANNER ───────────────────────────────────────────────────────────
with tab_scanner:
    col_left, col_right = st.columns([1, 1])

    with col_left:
        st.subheader("1. Cargar Ticket")

        limite_usuario_ok = db.puede_escanear(user_id)
        limite_ip_ok = db.puede_ip_analizar(ip_cliente)
        puede_analizar = limite_usuario_ok and limite_ip_ok

        if not limite_usuario_ok:
            st.warning(
                f"Has alcanzado el límite de {db.LIMITE_TICKETS_FREE} tickets este mes "
                "con el plan gratuito. El contador se reinicia el próximo mes."
            )
        elif not limite_ip_ok:
            st.warning(
                "Se han hecho demasiados análisis desde tu conexión en la última hora. "
                "Inténtalo de nuevo más tarde."
            )

        uploaded_file = st.file_uploader(
            "Selecciona o arrastra una imagen",
            type=["jpg", "jpeg", "png"],
            disabled=not puede_analizar,
        )

        if uploaded_file is not None:
            st.image(uploaded_file, caption="Vista previa del ticket", width=400)

            if st.button("🔍 Analizar con Gemini", type="primary", disabled=not puede_analizar):
                status_placeholder = st.empty()
                with st.spinner("Analizando tu ticket..."):
                    try:
                        import PIL.Image
                        import io
                        imagen = PIL.Image.open(io.BytesIO(uploaded_file.getvalue()))
                        chat = client.chats.create(model="models/gemini-3.5-flash")
                        response = analizar_ticket_con_reintento(
                            chat, PROMPT, imagen, status_placeholder
                        )
                        # La llamada a Gemini ya ha consumido cupo de la API en
                        # este punto (facturable o no), así que el contador se
                        # descuenta aquí, aunque el JSON posterior falle al parsear.
                        db.incrementar_contador(user_id)
                        db.registrar_analisis_ip(ip_cliente)
                        texto = response.text.replace("```json", "").replace("```", "").strip()
                        datos = json.loads(texto)
                        st.session_state["ticket_data"] = datos
                        status_placeholder.empty()
                        st.success("¡Ticket procesado correctamente!")
                        st.rerun()
                    except genai_errors.ClientError as e:
                        status_placeholder.empty()
                        if getattr(e, "code", None) == 429:
                            db.registrar_evento("analisis_error_429", user_id)
                            st.error(
                                "Se ha alcanzado el límite de frecuencia compartido de Gemini "
                                "tras varios intentos. Espera un minuto y vuelve a intentarlo."
                            )
                        else:
                            db.registrar_evento("analisis_error_otro", user_id)
                            st.error(f"Error al procesar el ticket: {str(e)}")
                    except genai_errors.ServerError:
                        status_placeholder.empty()
                        db.registrar_evento("analisis_error_503", user_id)
                        st.error(
                            "Gemini sigue saturado tras varios intentos. "
                            "Espera unos minutos y vuelve a intentarlo."
                        )
                    except Exception as e:
                        status_placeholder.empty()
                        db.registrar_evento("analisis_error_otro", user_id)
                        st.error(f"Error al procesar el ticket: {str(e)}")

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

            resumen = data.get("resumen_financiero", {})
            total = resumen.get("total", 0.0)
            st.metric(label="Total Calculado", value=f"{total:.2f} €")

            st.divider()
            if st.button("💾 Guardar en historial", type="primary"):
                entrada = {
                    "fecha_registro": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "tienda": meta.get("tienda", "Desconocida"),
                    "fecha_ticket": meta.get("fecha", ""),
                    "nif_cif": meta.get("nif_cif", ""),
                    "total": total,
                    "num_productos": len(items),
                    "items": items
                }
                db.guardar_ticket(user_id, entrada)
                st.success(f"✅ Ticket de {meta.get('tienda', 'tienda')} guardado en el historial.")
                st.rerun()
        else:
            st.info("Sube una imagen y pulsa 'Analizar con Gemini' para ver los resultados.")

# ─── TAB 2: HISTORIAL ─────────────────────────────────────────────────────────
with tab_historial:
    st.subheader("📋 Tickets guardados")

    historial = db.obtener_historial(user_id)

    if not historial:
        st.info("Aún no has guardado ningún ticket. Escanea uno y pulsa 'Guardar en historial'.")
    else:
        for entrada in historial:
            with st.expander(f"🧾 {entrada['tienda']} — {entrada['fecha_ticket']} — {entrada['total']:.2f} €"):
                st.markdown(f"**Registrado:** {entrada['fecha_registro']}")
                st.markdown(f"**CIF/NIF:** {entrada['nif_cif']}")
                st.markdown(f"**Productos:** {entrada['num_productos']}")
                if entrada["items"]:
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

        # Exportar CSV
        st.divider()
        if st.button("⬇️ Exportar historial a CSV"):
            filas = []
            for entrada in historial:
                for item in entrada["items"]:
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
                file_name=f"historial_gastos_{datetime.now().strftime('%Y%m')}.csv",
                mime="text/csv"
            )

# ─── TAB 3: RESUMEN MENSUAL ───────────────────────────────────────────────────
with tab_resumen:
    st.subheader("📊 Resumen de gastos")

    historial = db.obtener_historial(user_id)

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

# ─── PANEL DE MÉTRICAS (solo visible con la clave correcta en la URL) ─────────
# Ejemplo de acceso: http://localhost:8501/?clave=TU_CLAVE
# Define ADMIN_KEY en tu .env para activarlo; si no está definida, este
# bloque no se muestra nunca, ni siquiera intentando adivinar la URL.
ADMIN_KEY = os.environ.get("ADMIN_KEY")
if ADMIN_KEY and st.query_params.get("clave") == ADMIN_KEY:
    st.divider()
    with st.expander("📊 Panel de métricas (solo admin)", expanded=True):
        metricas = db.obtener_metricas()
        c1, c2, c3 = st.columns(3)
        c1.metric("Usuarios totales", metricas["total_usuarios"])
        c2.metric("Activos últimos 7 días", metricas["usuarios_activos_7d"])
        c3.metric("Activos últimos 30 días", metricas["usuarios_activos_30d"])
        c4, c5, c6 = st.columns(3)
        c4.metric("Análisis con éxito", metricas["total_analisis_ok"])
        c5.metric("Tickets guardados", metricas["total_tickets_guardados"])
        c6.metric("% análisis que se guardan", f"{metricas['tasa_guardado_pct']}%")