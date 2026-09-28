import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import firebase_admin
import streamlit as st
from dotenv import load_dotenv
from firebase_admin import credentials
from firebase_admin import firestore as fb_firestore
from google.cloud import firestore as gcf_firestore  # solo para firestore.Increment
from google.cloud.firestore_v1.base_query import FieldFilter

# Cargar variables de entorno locales (.env)
load_dotenv()


def _inicializar_firebase():
    """Igual que en la versión anterior: prueba primero el archivo local
    (entorno de pruebas), luego st.secrets (Streamlit Cloud), luego una
    variable de entorno. Sin esto, la app funciona en local pero no
    encuentra credenciales al desplegar."""
    if firebase_admin._apps:
        return fb_firestore.client()

    cred = None

    # Prioridad en local: si existe la clave de desarrollo, se usa esa
    # primero, para que tus pruebas nunca toquen los datos de producción.
    if os.path.exists("firebase_credentials_dev.json"):
        cred = credentials.Certificate("firebase_credentials_dev.json")
    elif os.path.exists("firebase_credentials.json"):
        cred = credentials.Certificate("firebase_credentials.json")
    elif os.path.exists("firebase-key.json"):
        cred = credentials.Certificate("firebase-key.json")

    if not cred:
        try:
            if "firebase" in st.secrets:
                cred = credentials.Certificate(dict(st.secrets["firebase"]))
        except Exception:
            pass

    if not cred:
        env_cred = os.environ.get("FIREBASE_CREDENTIALS")
        if env_cred:
            cred = credentials.Certificate(json.loads(env_cred))

    if not cred:
        raise RuntimeError(
            "No se encontraron credenciales de Firebase. Asegúrate de tener "
            "'firebase_credentials.json' en la raíz del proyecto, o los Secrets "
            "configurados en Streamlit Cloud."
        )

    firebase_admin.initialize_app(cred)
    return fb_firestore.client()


# Inicialización de cliente Firestore
db = _inicializar_firebase()

tickets_col = db.collection("tickets")
metricas_col = db.collection("metricas")
ip_analisis_col = db.collection("ip_analisis")
eventos_col = db.collection("eventos")

LIMITE_TICKETS_FREE = 20
LIMITE_ANALISIS_IP_POR_HORA = 15


ZONA_MADRID = ZoneInfo("Europe/Madrid")


def _ahora():
    # Fijamos la zona horaria explícitamente: el servidor de Streamlit
    # Cloud corre en UTC, así que datetime.now() sin zona quedaba 2h
    # (o 1h en invierno) por detrás de la hora real en España.
    return datetime.now(ZONA_MADRID).isoformat()


def _mes_actual() -> str:
    return datetime.now(ZONA_MADRID).strftime("%Y-%m")


def puede_escanear(user_id: str) -> bool:
    """Verifica si el usuario no ha superado el límite mensual."""
    doc = metricas_col.document(user_id).get()
    if not doc.exists:
        return True
    
    data = doc.to_dict()
    mes_actual = _mes_actual()
    
    if data.get("ultimo_mes_analisis") != mes_actual:
        return True
        
    analisis_mes = data.get("analisis_mes_actual", 0)
    return analisis_mes < LIMITE_TICKETS_FREE


def tickets_restantes(user_id: str) -> int:
    """Calcula los análisis disponibles del usuario en el mes en curso."""
    doc = metricas_col.document(user_id).get()
    if not doc.exists:
        return LIMITE_TICKETS_FREE
        
    data = doc.to_dict()
    mes_actual = _mes_actual()
    
    if data.get("ultimo_mes_analisis") != mes_actual:
        return LIMITE_TICKETS_FREE
        
    usados = data.get("analisis_mes_actual", 0)
    return max(0, LIMITE_TICKETS_FREE - usados)


def incrementar_contador(user_id: str) -> None:
    """Incrementa los contadores de uso del usuario."""
    doc_ref = metricas_col.document(user_id)
    doc = doc_ref.get()
    mes_actual = _mes_actual()
    
    if not doc.exists:
        doc_ref.set({
            "analisis_totales": 1,
            "analisis_mes_actual": 1,
            "ultimo_mes_analisis": mes_actual,
            "creado": _ahora(),
            "ultima_actividad": _ahora()
        })
    else:
        data = doc.to_dict()
        if data.get("ultimo_mes_analisis") != mes_actual:
            doc_ref.update({
                "analisis_mes_actual": 1,
                "ultimo_mes_analisis": mes_actual,
                "analisis_totales": gcf_firestore.Increment(1),
                "ultima_actividad": _ahora()
            })
        else:
            doc_ref.update({
                "analisis_mes_actual": gcf_firestore.Increment(1),
                "analisis_totales": gcf_firestore.Increment(1),
                "ultima_actividad": _ahora()
            })
    # Registrado fuera del if/else: cada análisis debe quedar con su propia
    # fecha, sea el primero del usuario o no — es lo que permite luego saber
    # en cuántos días distintos volvió cada persona.
    registrar_evento("analisis_ok", user_id)


def puede_ip_analizar(ip_cliente: str) -> bool:
    """Control de seguridad anti-abuso por dirección IP."""
    if not ip_cliente:
        return True
    doc = ip_analisis_col.document(ip_cliente).get()
    if not doc.exists:
        return True
    
    data = doc.to_dict()
    hace_una_hora = time.time() - 3600
    peticiones_recientes = [t for t in data.get("timestamps", []) if t > hace_una_hora]
    return len(peticiones_recientes) < LIMITE_ANALISIS_IP_POR_HORA


def registrar_analisis_ip(ip_cliente: str) -> None:
    """Registra la actividad de una IP."""
    if not ip_cliente:
        return
    doc_ref = ip_analisis_col.document(ip_cliente)
    doc = doc_ref.get()
    ahora = time.time()
    
    if not doc.exists:
        doc_ref.set({"timestamps": [ahora]})
    else:
        hace_una_hora = ahora - 3600
        peticiones = [t for t in doc.to_dict().get("timestamps", []) if t > hace_una_hora]
        peticiones.append(ahora)
        doc_ref.update({"timestamps": peticiones})


def ticket_existe_en_historial(user_id: str, hash_imagen: str = None) -> bool:
    """Comprueba si el ticket ya fue escaneado antes usando su hash de contenido."""
    if not hash_imagen:
        return False
    try:
        query = tickets_col.where(filter=FieldFilter("user_id", "==", user_id)).where(filter=FieldFilter("hash_imagen", "==", hash_imagen)).limit(1)
        docs = list(query.stream())
        return len(docs) > 0
    except Exception as e:
        print(f"Error al verificar duplicados en Firestore: {e}")
        return False


def guardar_ticket(user_id: str, entrada: dict) -> bool:
    """Guarda un ticket evitando duplicaciones si se reintenta una inserción con el mismo hash."""
    try:
        hash_img = entrada.get("hash_imagen")
        if hash_img and ticket_existe_en_historial(user_id, hash_img):
            return False  # Evita duplicado si ya fue guardado hace un instante

        entrada = {**entrada, "user_id": user_id, "creado": _ahora()}
        tickets_col.add(entrada)
        registrar_evento("ticket_guardado", user_id)
        return True
    except Exception as e:
        print(f"Error guardando ticket en Firestore: {e}")
        return False


def obtener_historial(user_id: str) -> list:
    """Obtiene el historial completo ordenado cronológicamente."""
    try:
        query = tickets_col.where(filter=FieldFilter("user_id", "==", user_id))
        docs = query.stream()
        historial = []
        for doc in docs:
            d = doc.to_dict()
            d["_doc_id"] = doc.id
            historial.append(d)
        
        def parsear_fecha_orden(item):
            val = item.get("creado", "")
            if hasattr(val, "isoformat"):
                return val.isoformat()
            return str(val or "")

        historial.sort(key=parsear_fecha_orden, reverse=True)
        return historial
    except Exception as e:
        print(f"Error consultando historial: {e}")
        return []


def borrar_ticket(user_id: str, doc_id: str) -> bool:
    """Elimina un ticket del usuario."""
    try:
        doc_ref = tickets_col.document(doc_id)
        doc = doc_ref.get()
        if doc.exists and doc.to_dict().get("user_id") == user_id:
            doc_ref.delete()
            return True
        return False
    except Exception as e:
        print(f"Error borrando ticket: {e}")
        return False


def registrar_evento(tipo_evento: str, user_id: str) -> None:
    """Métricas y eventos."""
    try:
        eventos_col.add({
            "tipo": tipo_evento,
            "user_id": user_id,
            "timestamp": _ahora()
        })
    except Exception as e:
        print(f"Error registrando evento: {e}")


def obtener_metricas() -> dict:
    """Métricas para administración, basadas en el registro de eventos
    'analisis_ok' (no en el contador agregado por usuario) — así se puede
    reconstruir en qué días distintos volvió cada persona, en vez de solo
    saber cuántos análisis hizo en total."""
    try:
        eventos_analisis = list(
            eventos_col.where(filter=FieldFilter("tipo", "==", "analisis_ok")).stream()
        )

        dias_por_usuario: dict[str, set] = {}
        for ev in eventos_analisis:
            d = ev.to_dict()
            uid = d.get("user_id")
            ts = d.get("timestamp")
            if not uid or not ts:
                continue
            fecha_str = ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)[:10]
            dias_por_usuario.setdefault(uid, set()).add(fecha_str)

        total_usuarios = len(list(metricas_col.stream()))
        activados = len(dias_por_usuario)
        volvieron_otro_dia = sum(1 for dias in dias_por_usuario.values() if len(dias) > 1)
        retencion_pct = round(100 * volvieron_otro_dia / activados, 1) if activados else 0.0
        dias_activos_promedio = (
            round(sum(len(d) for d in dias_por_usuario.values()) / activados, 1)
            if activados else 0.0
        )

        analisis_ok = len(eventos_analisis)
        total_guardados = len(list(tickets_col.stream()))
        tasa_guardado = round((total_guardados / analisis_ok * 100), 1) if analisis_ok else 0.0

        return {
            "total_usuarios": total_usuarios,
            "activados": activados,
            "total_analisis_ok": analisis_ok,
            "volvieron_otro_dia": volvieron_otro_dia,
            "retencion_pct": retencion_pct,
            "dias_activos_promedio": dias_activos_promedio,
            "tasa_guardado_pct": tasa_guardado
        }
    except Exception as e:
        print(f"Error obteniendo métricas: {e}")
        return {
            "total_usuarios": 0, "activados": 0, "total_analisis_ok": 0,
            "volvieron_otro_dia": 0, "retencion_pct": 0, "dias_activos_promedio": 0,
            "tasa_guardado_pct": 0
        }
