import base64
import hashlib
import inspect
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

# Streamlit reemplazó use_container_width=True por width="stretch". Se detecta qué
# versión hay instalada para no romper ni en local ni en Streamlit Cloud.
_param_width = inspect.signature(st.dataframe).parameters.get("width")
ANCHO_COMPLETO = (
    {"width": "stretch"}
    if _param_width is not None and _param_width.default == "stretch"
    else {"use_container_width": True}
)

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


def _num(valor) -> float:
    """Convierte a float tolerando None, NaN y textos; si no se puede, 0."""
    try:
        v = float(valor)
        return 0.0 if v != v else v
    except (TypeError, ValueError):
        return 0.0


def _fmt_cantidad(v) -> str:
    """1.0 -> '1', 0.5 -> '0.5' (sin los seis decimales por defecto)."""
    try:
        return f"{float(v):g}"
    except (TypeError, ValueError):
        return ""


def _estilo_fila_seleccion(fila):
    """Verde = se suma al historial; gris tachado = se muestra pero no se suma.
    Colores con transparencia para que se vean bien en tema claro y oscuro."""
    if str(fila["Estado"]).startswith("✅"):
        return ["background-color: rgba(46, 160, 67, 0.28)"] * len(fila)
    return ["color: rgba(128, 128, 128, 0.9); text-decoration: line-through; "
            "background-color: rgba(128, 128, 128, 0.12)"] * len(fila)


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

if "cola_tickets" not in st.session_state:
    st.session_state["cola_tickets"] = []

if "uploader_n" not in st.session_state:
    st.session_state["uploader_n"] = 0

if "version_seleccion" not in st.session_state:
    st.session_state["version_seleccion"] = 0

if "form_manual_n" not in st.session_state:
    st.session_state["form_manual_n"] = 0

if "aviso_manual" not in st.session_state:
    st.session_state["aviso_manual"] = None

if "aviso_tanda" not in st.session_state:
    st.session_state["aviso_tanda"] = None

# Encabezado principal
st.title("🧾 Procesador Inteligente de Tickets y Recibos")
st.caption("MVP · Gemini 3.6 Flash · Historial de gastos personal")
st.info(
    f"🧪 **Beta gratuita**: de momento puedes analizar hasta {db.LIMITE_TICKETS_FREE} documentos al mes. "
    "Cuando la app salga al público, el límite gratuito y las condiciones pueden cambiar."
)

# Métricas en el panel lateral (Sidebar)
restantes = db.tickets_restantes(user_id)
st.sidebar.metric("Tickets disponibles este mes", restantes)
st.sidebar.caption("🔑 Tu código de cuenta personal:")
st.sidebar.code(user_id, language=None)
st.sidebar.caption("💡 Guarda este código. Si borras las cookies o cambias de dispositivo, puedes ingresarlo aquí abajo para recuperar tu historial completo:")
introducir_codigo_manual(cookies)


# ─── FUNCIONES AUXILIARES DE PROCESAMIENTO ─────────────────────────────────────
MAX_TICKETS_POR_TANDA = 5


def analizar_documento(uploaded_file, hash_actual):
    """Analiza UN documento con Gemini.
    Devuelve (datos, None) si va bien o (None, mensaje_de_error) si falla."""
    status_placeholder = st.empty()
    try:
        imagen_optimizada = convertir_a_imagen_pil(uploaded_file)
        chat = client.chats.create(model="models/gemini-3.6-flash")
        response = analizar_ticket_con_reintento(
            chat, PROMPT, imagen_optimizada, status_placeholder
        )

        # Gemini respondió: descontar uso e incrementar contadores
        # Tokens que Gemini declara haber usado (la salida incluye el razonamiento)
        uso = getattr(response, "usage_metadata", None)
        t_in = getattr(uso, "prompt_token_count", None)
        t_total = getattr(uso, "total_token_count", None)
        t_out = (t_total - t_in) if isinstance(t_in, int) and isinstance(t_total, int) else None
        db.incrementar_contador(user_id, t_in, t_out)
        db.registrar_analisis_ip(ip_cliente)
        st.session_state["hashes_analizados"].add(hash_actual)

        texto = response.text.replace("```json", "").replace("```", "").strip()
        datos = json.loads(texto)
        status_placeholder.empty()
        return datos, None
    except genai_errors.ClientError as e:
        status_placeholder.empty()
        if getattr(e, "code", None) == 429:
            db.registrar_evento("analisis_error_429", user_id)
            return None, "Límite de frecuencia de Gemini alcanzado."
        db.registrar_evento("analisis_error_otro", user_id)
        return None, f"Error al procesar el documento: {e}"
    except genai_errors.ServerError:
        status_placeholder.empty()
        db.registrar_evento("analisis_error_503", user_id)
        return None, "Gemini está saturado."
    except Exception as e:
        status_placeholder.empty()
        db.registrar_evento("analisis_error_otro", user_id)
        return None, f"Error al procesar el documento: {e}"


def procesar_tanda(archivos, permitir_duplicados):
    """Analiza varios archivos en secuencia y deja los resultados en la cola de revisión."""
    resultados, errores, duplicados_omitidos, sin_cupo = [], [], [], []
    barra = st.progress(0.0, text="Preparando...")

    for i, f in enumerate(archivos):
        barra.progress(i / len(archivos), text=f"Analizando {i + 1} de {len(archivos)}: {f.name}")
        h = calcular_hash_archivo(f)

        es_dup = h in st.session_state["hashes_analizados"] or db.ticket_existe_en_historial(user_id, h)
        if es_dup and not permitir_duplicados:
            duplicados_omitidos.append(f.name)
            continue

        # El cupo puede agotarse a mitad de tanda: se comprueba antes de cada llamada
        if not (
            db.puede_escanear(user_id)
            and db.puede_ip_analizar(ip_cliente)
            and db.puede_analizar_global()
        ):
            sin_cupo.append(f.name)
            continue

        datos, error = analizar_documento(f, h)
        if error:
            errores.append(f"{f.name}: {error}")
        else:
            resultados.append({"nombre": f.name, "hash": h, "datos": datos})

    barra.progress(1.0, text="Tanda terminada")

    # Cola de revisión: el primero pasa a revisión si no hay otro en curso
    cola = st.session_state["cola_tickets"]
    cola.extend(resultados)
    if st.session_state["ticket_data"] is None and cola:
        siguiente = cola.pop(0)
        st.session_state["ticket_data"] = siguiente["datos"]
        st.session_state["hash_imagen_actual"] = siguiente["hash"]
        st.session_state["nombre_ticket_actual"] = siguiente["nombre"]

    st.session_state["aviso_tanda"] = {
        "ok": len(resultados), "errores": errores,
        "duplicados": duplicados_omitidos, "sin_cupo": sin_cupo,
    }
    st.session_state["uploader_n"] += 1  # vacía el selector de archivos
    st.rerun()


def pasar_al_siguiente_ticket():
    """Carga el siguiente ticket de la cola (o deja el panel vacío)."""
    cola = st.session_state["cola_tickets"]
    if cola:
        siguiente = cola.pop(0)
        st.session_state["ticket_data"] = siguiente["datos"]
        st.session_state["hash_imagen_actual"] = siguiente["hash"]
        st.session_state["nombre_ticket_actual"] = siguiente["nombre"]
    else:
        st.session_state["ticket_data"] = None
        st.session_state["nombre_ticket_actual"] = None


# Pestañas principales
tab_scanner, tab_manual, tab_historial, tab_resumen, tab_premium = st.tabs([
    "📷 Escanear Documento",
    "✍️ Gasto manual",
    "📋 Historial",
    "📊 Resumen Mensual",
    "⭐ Premium (próximamente)"
])

# ─── TAB 1: SCANNER ───────────────────────────────────────────────────────────
with tab_scanner:
    col_left, col_right = st.columns([1, 1])

    with col_left:
        st.subheader("1. Cargar Ticket o Recibo")

        limite_usuario_ok = db.puede_escanear(user_id)
        limite_ip_ok = db.puede_ip_analizar(ip_cliente)
        limite_global_ok = db.puede_analizar_global()
        puede_analizar = limite_usuario_ok and limite_ip_ok and limite_global_ok

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
        elif not limite_global_ok:
            st.warning(
                "La app ha alcanzado hoy su capacidad máxima de análisis en la beta. "
                "Vuelve mañana; tu historial sigue disponible."
            )

        restantes_num = db.tickets_restantes(user_id)
        cupo_tanda = MAX_TICKETS_POR_TANDA
        if isinstance(restantes_num, int):
            cupo_tanda = max(0, min(MAX_TICKETS_POR_TANDA, restantes_num))

        st.markdown(
            f"📸 **Puedes subir varios tickets a la vez (hasta {MAX_TICKETS_POR_TANDA}).** "
            "Pulsa **Browse files** y, en la galería del móvil, **mantén pulsada una foto** y marca "
            "las demás (en ordenador, también puedes arrastrarlas aquí)."
        )
        archivos = st.file_uploader(
            f"Fotos de tickets (JPG, PNG) o recibos PDF · hasta {MAX_TICKETS_POR_TANDA} a la vez",
            type=["jpg", "jpeg", "png", "pdf"],
            accept_multiple_files=True,
            disabled=not puede_analizar,
            key=f"uploader_{st.session_state['uploader_n']}",
        )

        if archivos and len(archivos) > MAX_TICKETS_POR_TANDA:
            st.warning(
                f"Has seleccionado {len(archivos)} archivos. Solo se analizarán los "
                f"primeros {MAX_TICKETS_POR_TANDA} en cada tanda."
            )
        archivos_tanda = (archivos or [])[:MAX_TICKETS_POR_TANDA]

        if archivos_tanda:
            if isinstance(restantes_num, int) and len(archivos_tanda) > cupo_tanda:
                st.warning(
                    f"Te quedan {restantes_num} análisis este mes: se procesarán solo los "
                    f"primeros {cupo_tanda}."
                )

            # Vista previa compacta + aviso de duplicados
            n_dup = 0
            for f in archivos_tanda:
                h = calcular_hash_archivo(f)
                dup = h in st.session_state["hashes_analizados"] or db.ticket_existe_en_historial(user_id, h)
                n_dup += dup
                etiqueta = f"{'📄' if f.name.lower().endswith('.pdf') else '🖼️'} {f.name}"
                st.caption(etiqueta + ("  ⚠️ ya registrado" if dup else ""))
            if len(archivos_tanda) == 1 and not archivos_tanda[0].name.lower().endswith(".pdf"):
                st.image(archivos_tanda[0], caption="Vista previa del documento", width=300)

            permitir_dup = False
            if n_dup:
                permitir_dup = st.checkbox(
                    f"Analizar también los {n_dup} ya registrados (consumen cuota)",
                    key=f"permitir_dup_{st.session_state['uploader_n']}",
                )
                if not permitir_dup:
                    st.info("Los documentos ya registrados se omitirán y no gastan cuota.")

            etiqueta_boton = (
                "🔍 Analizar con Gemini" if len(archivos_tanda) == 1
                else f"🔍 Analizar {len(archivos_tanda)} documentos"
            )
            if st.button(etiqueta_boton, type="primary", disabled=not puede_analizar):
                procesar_tanda(archivos_tanda[:max(cupo_tanda, 0)] if isinstance(restantes_num, int) else archivos_tanda, permitir_dup)

    with col_right:
        st.subheader("2. Resultados y Auditoría")

        aviso = st.session_state.get("aviso_tanda")
        if aviso:
            if aviso["ok"]:
                st.success(f"✅ {aviso['ok']} documento(s) analizado(s) correctamente.")
            if aviso["duplicados"]:
                st.info("Omitidos por estar ya registrados: " + ", ".join(aviso["duplicados"]))
            if aviso["sin_cupo"]:
                st.warning("Sin cupo suficiente para: " + ", ".join(aviso["sin_cupo"]))
            for err in aviso["errores"]:
                st.error(err)
            st.session_state["aviso_tanda"] = None

        if st.session_state["ticket_data"] is not None:
            data = st.session_state["ticket_data"]
            pendientes = len(st.session_state["cola_tickets"])
            nombre_actual = st.session_state.get("nombre_ticket_actual")
            if pendientes:
                st.info(
                    f"📥 Revisando **{nombre_actual or 'ticket'}** · "
                    f"quedan {pendientes} más en cola"
                )
            elif nombre_actual:
                st.caption(f"Revisando: {nombre_actual}")
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
                if "incluir" not in df.columns:
                    df.insert(0, "incluir", True)
                if "mi_parte" not in df.columns:
                    df["mi_parte"] = 100.0
                df["mi_parte"] = pd.to_numeric(df["mi_parte"], errors="coerce").fillna(100.0)
                orden = ["incluir", "concepto_raw", "cantidad", "precio_total", "mi_parte"]
                resto = [c for c in expected_cols if c in df.columns and c not in orden]
                df = df[[c for c in orden if c in df.columns] + resto]
                df["incluir"] = df["incluir"].fillna(True).astype(bool)

                st.write(
                    "**Productos detectados (editable)** — marca los que quieres sumar a tu historial. "
                    "¿Pagaste a medias? Cambia **Mi parte (%)** (50 = la mitad)."
                )
                edited_df = st.data_editor(
                    df, num_rows="dynamic",
                    **ANCHO_COMPLETO,
                    column_config={
                        "incluir": st.column_config.CheckboxColumn(
                            "Incluir", default=True,
                            help="Si lo desmarcas, el producto se sigue mostrando pero no se suma a tu gasto.",
                        ),
                        "mi_parte": st.column_config.NumberColumn(
                            "Mi parte (%)", default=100.0, min_value=0.0, max_value=100.0, step=5.0,
                            help="Porcentaje del producto que pagas tú. 50 = la mitad (p. ej. lo compartiste con otra persona).",
                        ),
                    },
                    key=f"editor_items_{st.session_state.get('hash_imagen_actual')}_{st.session_state['version_seleccion']}"
                )
                edited_df["incluir"] = edited_df["incluir"].fillna(True).astype(bool)
                edited_df["mi_parte"] = pd.to_numeric(edited_df["mi_parte"], errors="coerce").fillna(100.0).clip(0, 100)
                items = edited_df.to_dict(orient="records")

                # Atajo: dividir todo el ticket a partes iguales
                cd1, cd2, _ = st.columns([2, 1, 1])
                n_pers = cd1.selectbox(
                    "Pagado entre varias personas (a partes iguales)",
                    [1, 2, 3, 4, 5, 6], format_func=lambda n: "Solo yo" if n == 1 else f"Entre {n} personas",
                    key=f"n_pers_{st.session_state['version_seleccion']}",
                )
                if cd2.button("Aplicar", key=f"aplicar_div_{st.session_state['version_seleccion']}"):
                    data["items"] = [{**it, "mi_parte": round(100 / n_pers, 2)} for it in items]
                    st.session_state["version_seleccion"] += 1
                    st.rerun()

                # Marcar / desmarcar todos conservando las ediciones ya hechas
                cb1, cb2, _ = st.columns([1, 1, 2])
                for col_b, texto, valor in ((cb1, "☑️ Marcar todos", True), (cb2, "⬜ Desmarcar todos", False)):
                    if col_b.button(texto, key=f"sel_{int(valor)}_{st.session_state['version_seleccion']}"):
                        data["items"] = [{**it, "incluir": valor} for it in items]
                        st.session_state["version_seleccion"] += 1
                        st.rerun()

                # Vista previa con color: verde = incluido, gris tachado = excluido
                st.caption("Vista previa: 🟩 verde = se suma a tu historial · ⬜ gris tachado = se muestra, pero no se suma")
                vista = pd.DataFrame([{
                    "Estado": "✅ Incluido" if it.get("incluir", True) else "⬜ Excluido",
                    "Producto": it.get("valor_final") or it.get("concepto_raw") or "",
                    "Cantidad": it.get("cantidad"),
                    "Total (€)": _num(it.get("precio_total")),
                    "Mi parte (%)": _num(it.get("mi_parte", 100)),
                    "Mi parte (€)": round(_num(it.get("precio_total")) * _num(it.get("mi_parte", 100)) / 100, 2),
                } for it in items])
                st.dataframe(
                    vista.style.apply(_estilo_fila_seleccion, axis=1).format(
                        {"Total (€)": "{:.2f}", "Mi parte (€)": "{:.2f}", "Mi parte (%)": "{:.0f}", "Cantidad": _fmt_cantidad}),
                    **ANCHO_COMPLETO, hide_index=True,
                )

            resumen = data.get("resumen_financiero", {})
            total_original = _num(resumen.get("total", 0.0))
            incluidos = [it for it in items if it.get("incluir", True)]
            todos_incluidos = len(incluidos) == len(items)
            todo_entero = all(_num(it.get("mi_parte", 100)) >= 100 for it in incluidos)
            # Con todo marcado y al 100 % se respeta el total del ticket (descuentos, redondeos);
            # en otro caso, la suma de tu parte de los productos marcados.
            total = (
                total_original if (todos_incluidos and todo_entero)
                else round(sum(_num(it.get("precio_total")) * _num(it.get("mi_parte", 100)) / 100 for it in incluidos), 2)
            )

            mt1, mt2 = st.columns(2)
            mt1.metric("Total del ticket", f"{total_original:.2f} €")
            mt2.metric(
                "Se sumará a tu historial", f"{total:.2f} €",
                help=f"{len(incluidos)} de {len(items)} productos incluidos",
            )
            sin_seleccion = bool(items) and not incluidos
            if sin_seleccion:
                st.warning("No has marcado ningún producto. Marca al menos uno para poder guardar.")

            st.divider()
            
            firma_ticket = hashlib.sha1(
                json.dumps({"meta": meta, "total": total, "items": items}, sort_keys=True, default=str).encode()
            ).hexdigest()
            
            ya_guardado = st.session_state.get("ultimo_guardado_firma") == firma_ticket

            # Duplicado por CONTENIDO (otra foto del mismo ticket): el hash solo
            # detecta archivos idénticos, esto compara tienda + fecha + total.
            similar = db.buscar_ticket_similar(
                user_id, meta.get("tienda"), meta.get("fecha"), total_original, items
            )
            confirmar_dup = True
            _admin = os.environ.get("ADMIN_KEY")
            if _admin and st.query_params.get("clave") == _admin:
                with st.expander("🔍 Diagnóstico de duplicados (solo admin)"):
                    st.caption(
                        f"Lectura actual → tienda: {meta.get('tienda')!r} · fecha: {meta.get('fecha')!r} · "
                        f"total: {total_original} · productos: {len(items)}"
                    )
                    diag = db.diagnostico_duplicado(
                        user_id, meta.get("tienda"), meta.get("fecha"), total_original, items
                    )
                    st.dataframe(pd.DataFrame(diag), **ANCHO_COMPLETO) if diag else st.write("Historial vacío.")
            if similar:
                st.warning(
                    f"⚠️ **Parece que este ticket ya está en tu historial**: "
                    f"{similar.get('tienda')} · {similar.get('fecha_ticket')} · "
                    f"{float(similar.get('total', 0)):.2f} € "
                    f"(registrado el {similar.get('fecha_registro')}). "
                    "Puede ser otra foto del mismo ticket."
                )
                confirmar_dup = st.checkbox(
                    "Es un ticket distinto, guardar de todas formas",
                    key=f"confirmar_dup_{st.session_state.get('hash_imagen_actual')}",
                )

            # BOTÓN CORREGIDO: Guarda directamente sin colisión de callbacks ni congelamientos
            if st.button(
                "💾 Guardar en historial", 
                type="primary", 
                disabled=ya_guardado or st.session_state["guardando_ticket"] or not confirmar_dup or sin_seleccion
            ):
                st.session_state["guardando_ticket"] = True
                
                entrada = {
                    "fecha_registro": datetime.now(ZONA_MADRID).strftime("%Y-%m-%d %H:%M"),
                    "tienda": meta.get("tienda", "Desconocida"),
                    "fecha_ticket": meta.get("fecha", ""),
                    "nif_cif": meta.get("nif_cif", ""),
                    "total": total,
                    "total_original": total_original,
                    "num_productos": len(items),
                    "num_incluidos": len(incluidos),
                    "items": [
                        {**{k: v for k, v in it.items() if k != "incluir"},
                         "incluido": bool(it.get("incluir", True))}
                        for it in items
                    ],
                    "hash_imagen": st.session_state.get("hash_imagen_actual")
                }
                
                with st.spinner("Guardando en la base de datos..."):
                    exito = db.guardar_ticket(user_id, entrada)
                
                st.session_state["guardando_ticket"] = False
                
                if exito:
                    st.session_state["ultimo_guardado_firma"] = firma_ticket
                    pasar_al_siguiente_ticket()
                    st.toast(f"✅ Ticket de {meta.get('tienda', 'tienda')} guardado correctamente.")
                    st.rerun()
                else:
                    st.warning("Este ticket ya se encuentra registrado en la base de datos.")

            if st.button("🗑️ Descartar este ticket (sin guardar)"):
                pasar_al_siguiente_ticket()
                st.rerun()
        else:
            st.info("Sube una imagen o PDF y pulsa 'Analizar con Gemini' para ver los resultados.")

# ─── TAB: GASTO MANUAL ────────────────────────────────────────────────────────
with tab_manual:
    st.subheader("✍️ Añadir un gasto sin ticket")
    st.caption(
        "Para compras pequeñas en las que no te dan ticket o se te olvidó pedirlo. "
        "Cuenta igual que los demás en tu historial, resumen y CSV, y no gasta análisis."
    )

    if st.session_state.get("aviso_manual"):
        st.success(st.session_state["aviso_manual"])
        st.session_state["aviso_manual"] = None

    n = st.session_state["form_manual_n"]
    hoy = datetime.now(ZONA_MADRID).date()

    cm1, cm2 = st.columns(2)
    fecha_m = cm1.date_input("Fecha", value=hoy, max_value=hoy, format="DD/MM/YYYY", key=f"man_fecha_{n}")
    importe_m = cm2.number_input("Importe (€)", min_value=0.0, step=0.5, format="%.2f", key=f"man_importe_{n}")
    tienda_m = st.text_input(
        "Tienda o concepto", max_chars=80,
        placeholder="Ej.: Bazar de la esquina, café, parking...", key=f"man_tienda_{n}",
    )
    nota_m = st.text_input("Nota (opcional)", max_chars=200, key=f"man_nota_{n}")

    fecha_txt = fecha_m.strftime("%d/%m/%Y")
    valido_m = bool(tienda_m.strip()) and importe_m > 0

    confirmar_m = True
    if valido_m:
        similar_m = db.buscar_ticket_similar(user_id, tienda_m, fecha_txt, importe_m)
        if similar_m:
            st.warning(
                f"⚠️ Ya tienes un gasto parecido: {similar_m.get('tienda')} · "
                f"{similar_m.get('fecha_ticket')} · {float(similar_m.get('total', 0)):.2f} €."
            )
            confirmar_m = st.checkbox("Es un gasto distinto, guardar de todas formas", key=f"man_conf_{n}")

    if st.button("💾 Guardar gasto", type="primary", disabled=not valido_m or not confirmar_m, key=f"man_guardar_{n}"):
        concepto = tienda_m.strip()
        entrada = {
            "fecha_registro": datetime.now(ZONA_MADRID).strftime("%Y-%m-%d %H:%M"),
            "tienda": concepto,
            "fecha_ticket": fecha_txt,
            "nif_cif": "",
            "total": round(float(importe_m), 2),
            "num_productos": 1,
            "items": [{
                "concepto_raw": concepto,
                "cantidad": 1.0,
                "precio_unitario": round(float(importe_m), 2),
                "precio_total": round(float(importe_m), 2),
                "gemini_original": "",
                "accion_usuario": "MANUAL",
                "valor_final": concepto,
            }],
            "origen": "manual",
            "nota": nota_m.strip(),
            "hash_imagen": None,
        }
        if db.guardar_ticket(user_id, entrada):
            st.session_state["aviso_manual"] = f"✅ Gasto de {importe_m:.2f} € en «{concepto}» guardado."
            st.session_state["form_manual_n"] += 1  # vacía el formulario
            st.rerun()
        else:
            st.error("No se ha podido guardar el gasto. Inténtalo de nuevo.")

# Obtener historial de Firestore
historial = db.obtener_historial(user_id)

# ─── TAB 2: HISTORIAL ─────────────────────────────────────────────────────────
with tab_historial:
    st.subheader("📋 Tickets guardados")

    if not historial:
        st.info("Aún no has guardado ningún ticket. Escanea uno y pulsa 'Guardar en historial'.")
    else:
        for entrada in historial:
            icono = "✍️" if entrada.get("origen") == "manual" else "🧾"
            with st.expander(f"{icono} {entrada['tienda']} — {entrada['fecha_ticket']} — {entrada['total']:.2f} €"):
                st.markdown(f"**Registrado:** {entrada['fecha_registro']}")
                if entrada.get("origen") == "manual":
                    st.markdown("**Origen:** gasto manual (sin ticket)")
                    if entrada.get("nota"):
                        st.markdown(f"**Nota:** {entrada['nota']}")
                else:
                    st.markdown(f"**CIF/NIF:** {entrada['nif_cif']}")
                n_inc = entrada.get("num_incluidos", entrada["num_productos"])
                if n_inc != entrada["num_productos"]:
                    st.markdown(
                        f"**Productos:** {entrada['num_productos']} en el ticket · "
                        f"{n_inc} incluidos en tu gasto"
                    )
                    if entrada.get("total_original") is not None:
                        st.markdown(f"**Total del ticket completo:** {float(entrada['total_original']):.2f} €")
                else:
                    st.markdown(f"**Productos:** {entrada['num_productos']}")
                if entrada.get("items"):
                    df_items = pd.DataFrame(entrada["items"])
                    if "incluido" in df_items.columns:
                        df_items["incluido"] = df_items["incluido"].map(lambda v: "✅" if v is not False else "⬜")
                    cols = ["incluido", "concepto_raw", "cantidad", "precio_unitario", "precio_total", "valor_final"]
                    df_items = df_items[[c for c in cols if c in df_items.columns]]
                    st.dataframe(df_items, **ANCHO_COMPLETO)

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
                    if item.get("incluido", True) is False:
                        continue  # el CSV recoge solo lo que cuenta como gasto
                    filas.append({
                        "origen": entrada.get("origen", "ticket"),
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
        st.dataframe(df_por_tienda, **ANCHO_COMPLETO)

        st.divider()
        st.write("**Últimos tickets:**")
        st.dataframe(df_resumen.sort_values("fecha", ascending=False), **ANCHO_COMPLETO)

# ─── TAB 4: PREMIUM (PAYWALL FICTICIO) ────────────────────────────────────────
with tab_premium:
    st.subheader("⭐ Funciones en estudio para una futura versión Premium")
    st.write(
        "Todavía no existen: estamos decidiendo cuáles construir. "
        "Marca las que te resultarían útiles. Tu opinión decide qué se desarrolla primero."
    )

    if "interes_enviado" not in st.session_state:
        st.session_state["interes_enviado"] = set()

    for clave, descripcion in db.FUNCIONES_PREMIUM.items():
        c_txt, c_btn = st.columns([4, 1])
        c_txt.write(descripcion)
        enviado = clave in st.session_state["interes_enviado"]
        if c_btn.button(
            "✅ Anotado" if enviado else "👍 Me interesa",
            key=f"interes_{clave}", disabled=enviado,
        ):
            db.registrar_interes_premium(user_id, f"interes_premium:{clave}")
            st.session_state["interes_enviado"].add(clave)
            st.rerun()

    st.divider()
    st.write("**Si existieran las funciones que has marcado, ¿cuánto pagarías al mes?**")
    precio = st.radio(
        "Precio mensual", list(db.OPCIONES_PRECIO.keys()),
        format_func=lambda k: db.OPCIONES_PRECIO[k],
        index=None, label_visibility="collapsed",
    )
    if st.button("Enviar respuesta", disabled=precio is None or st.session_state.get("precio_enviado", False)):
        db.registrar_interes_premium(user_id, f"precio_premium:{precio}")
        st.session_state["precio_enviado"] = True
        st.success("¡Gracias! Tu respuesta se ha guardado.")

# ─── PANEL ADMINISTRADOR ──────────────────────────────────────────────────────
ADMIN_KEY = os.environ.get("ADMIN_KEY")
if ADMIN_KEY and st.query_params.get("clave") == ADMIN_KEY:
    st.divider()
    with st.expander("📊 Panel de métricas (solo admin)", expanded=True):
        _op_nuevos = f"🆕 Usuarios nuevos desde el {db.INICIO_MEDICION}"
        _op_todos = "📚 Todo el histórico"
        vista_metricas = st.radio(
            "Qué medir", [_op_nuevos, _op_todos], horizontal=True, label_visibility="collapsed",
        )
        st.caption(
            "Usuarios nuevos = personas cuyo primer análisis fue a partir de esa fecha. "
            "Los testers anteriores quedan fuera, pero conservan sus datos."
            if vista_metricas == _op_nuevos else
            "Incluye a todos los usuarios desde el principio, también a los primeros testers."
        )
        metricas = db.obtener_metricas(desde=db.INICIO_MEDICION if vista_metricas == _op_nuevos else None)

        c1, c2, c3 = st.columns(3)
        c1.metric("Usuarios totales", metricas["total_usuarios"])
        c2.metric("Activados", metricas["activados"], help="Analizaron al menos un ticket")
        c3.metric("Análisis con éxito", metricas["total_analisis_ok"])

        c4, c5, c6, c7 = st.columns(4)
        c4.metric("Volvieron otro día", metricas["volvieron_otro_dia"])
        c5.metric("Retención (% de activados)", f"{metricas['retencion_pct']}%")
        c6.metric("Días activos / usuario", metricas["dias_activos_promedio"])
        c7.metric("% análisis que se guardan", f"{metricas['tasa_guardado_pct']}%")

        c8, c9, _, _ = st.columns(4)
        c8.metric(
            "Activos últimos 7 días", metricas["activos_7d"],
            help="Usuarios con al menos un análisis en los últimos 7 días",
        )
        c9.metric("Gastos manuales", metricas["gastos_manuales"], help="Gastos añadidos sin ticket")

        # Coste de Gemini: medido con los tokens reales de cada análisis
        uso_g = db.obtener_uso_global()
        k1, k2, k3, k4 = st.columns(4)
        k1.metric(
            "Coste medio por ticket", f"{metricas['coste_medio_usd'] * 100:.2f} cént. USD",
            help=f"Medido en {metricas['analisis_con_coste']} análisis · "
                 f"{metricas['tokens_medios_entrada']} tokens de entrada y "
                 f"{metricas['tokens_medios_salida']} de salida de media (incluye razonamiento).",
        )
        k2.metric(
            "Gasto estimado del mes", f"{uso_g['coste_mes_usd']:.2f} USD",
            help=f"Presupuesto mensual: {db.PRESUPUESTO_MENSUAL_USD:.0f} USD. Al alcanzarlo, la app deja de analizar.",
        )
        k3.metric("Análisis hoy (todos)", f"{uso_g['analisis_hoy']} / {db.LIMITE_GLOBAL_DIARIO}")

        st.divider()
        st.write("**Interés en Premium (usuarios distintos):**")
        interes = db.obtener_interes_premium()
        cols_f = st.columns(len(db.FUNCIONES_PREMIUM))
        for col, clave in zip(cols_f, db.FUNCIONES_PREMIUM):
            col.metric(clave.replace("_", " ").capitalize(), interes.get(f"interes_premium:{clave}", 0))
        cols_p = st.columns(len(db.OPCIONES_PRECIO))
        for col, (clave, etiqueta) in zip(cols_p, db.OPCIONES_PRECIO.items()):
            col.metric(f"Pagaría: {etiqueta.split(',')[0] if clave == '0' else etiqueta}", interes.get(f"precio_premium:{clave}", 0))
