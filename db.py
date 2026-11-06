import json
import os
import re
import time
from datetime import datetime, timedelta
from difflib import SequenceMatcher
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
config_col = db.collection("config")

# Beta: límite mensual alto (casi ilimitado para un usuario normal). Al salir
# al público, bajarlo y atarlo al plan de pago.
LIMITE_TICKETS_FREE = 100
LIMITE_ANALISIS_IP_POR_HORA = 15
# Cortacircuitos: máximo de análisis al día entre TODOS los usuarios. Protege la
# cuota compartida de Gemini (5 RPM) y la factura si algún día hay facturación.
# Ajusta el valor a lo que permita tu cuota diaria real en AI Studio.
LIMITE_GLOBAL_DIARIO = 200

# Funciones candidatas a Premium (paywall ficticio para medir interés)
FUNCIONES_PREMIUM = {
    "comparador_precios": "Comparador de precios: cómo cambia el precio de un mismo producto entre fechas y tiendas",
    "dividir_pagos": "Dividir pagos: marca qué productos pagaste tú y cuáles otra persona, y calcula cuánto debe cada uno",
    "informes_mensuales": "Informes mensuales: resumen por producto y tienda, comparado con meses anteriores",
}
OPCIONES_PRECIO = {"0": "Nada, solo usaría la versión gratuita", "1.99": "1,99 €/mes", "2.99": "2,99 €/mes", "4.99": "4,99 €/mes"}


ZONA_MADRID = ZoneInfo("Europe/Madrid")


def _ahora():
    # Fijamos la zona horaria explícitamente: el servidor de Streamlit
    # Cloud corre en UTC, así que datetime.now() sin zona quedaba 2h
    # (o 1h en invierno) por detrás de la hora real en España.
    return datetime.now(ZONA_MADRID).isoformat()


def _mes_actual() -> str:
    return datetime.now(ZONA_MADRID).strftime("%Y-%m")


def _hoy() -> str:
    return datetime.now(ZONA_MADRID).strftime("%Y-%m-%d")


def puede_analizar_global() -> bool:
    """Cortacircuitos: False si ya se alcanzó el máximo diario entre todos los usuarios."""
    try:
        doc = config_col.document("uso_global").get()
        if not doc.exists:
            return True
        data = doc.to_dict()
        if data.get("dia") != _hoy():
            return True
        return data.get("analisis_dia", 0) < LIMITE_GLOBAL_DIARIO
    except Exception as e:
        print(f"Error comprobando límite global: {e}")
        return True  # ante un fallo de lectura, no bloquear a los usuarios


def registrar_analisis_global() -> None:
    try:
        ref = config_col.document("uso_global")
        doc = ref.get()
        hoy = _hoy()
        if doc.exists and doc.to_dict().get("dia") == hoy:
            ref.update({"analisis_dia": gcf_firestore.Increment(1)})
        else:
            ref.set({"dia": hoy, "analisis_dia": 1})
    except Exception as e:
        print(f"Error registrando uso global: {e}")


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
    registrar_analisis_global()
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
        # Los gastos manuales no pasan por Gemini: se registran con otro tipo de
        # evento para no distorsionar el "% de análisis que se guardan".
        es_manual = entrada.get("origen") == "manual"
        registrar_evento("gasto_manual" if es_manual else "ticket_guardado", user_id)
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


def _normalizar_fecha(texto) -> str:
    """Lleva fechas como '12/10/25', '12-10-2025' o '2025-10-12' a 'YYYY-MM-DD'.
    Si no se reconoce el formato, devuelve el texto limpio."""
    t = str(texto or "").strip()
    m = re.search(r"(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})", t)
    if m:
        a, mes, d = m.groups()
        return f"{int(a):04d}-{int(mes):02d}-{int(d):02d}"
    m = re.search(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})", t)
    if m:
        d, mes, a = m.groups()
        a = int(a) + 2000 if int(a) < 100 else int(a)
        return f"{a:04d}-{int(mes):02d}-{int(d):02d}"
    return t.lower()


def _normalizar_tienda(texto) -> str:
    return re.sub(r"[^a-z0-9]", "", str(texto or "").lower())


def _tiendas_parecidas(a: str, b: str) -> bool:
    na, nb = _normalizar_tienda(a), _normalizar_tienda(b)
    if not na or not nb:
        return False
    if na in nb or nb in na:
        return True
    return SequenceMatcher(None, na, nb).ratio() >= 0.7


def _precios(items) -> list:
    """Lista de precios (redondeados, sin ceros) de los productos de un ticket."""
    out = []
    for it in items or []:
        try:
            v = round(float(it.get("precio_total")), 2)
        except (TypeError, ValueError):
            continue
        if v == v and v > 0:
            out.append(v)
    return out


def _solape_precios(a: list, b: list) -> float:
    """Proporción de precios que coinciden entre dos tickets (0 a 1). Los precios
    se leen igual aunque la foto cambie, a diferencia de la fecha o el total,
    que Gemini a veces lee distinto según el encuadre."""
    if not a or not b:
        return 0.0
    from collections import Counter
    ca, cb = Counter(a), Counter(b)
    comunes = sum(min(ca[k], cb[k]) for k in ca)
    return comunes / max(len(a), len(b))


def _senales_similitud(t: dict, tienda, fecha_n: str, total: float, precios: list) -> dict:
    """Señales de que el ticket guardado 't' es el mismo que el que se está analizando."""
    try:
        total_t = float(t.get("total", 0))
    except (TypeError, ValueError):
        total_t = 0.0
    try:
        total_orig_t = float(t.get("total_original", total_t))
    except (TypeError, ValueError):
        total_orig_t = total_t
    # Se compara con el total guardado Y con el del ticket completo (si se guardó
    # con selección parcial, 'total' es solo lo marcado).
    total_ok = total > 0 and (abs(total_t - total) < 0.011 or abs(total_orig_t - total) < 0.011)
    fecha_t = _normalizar_fecha(t.get("fecha_ticket"))
    fecha_ok = bool(fecha_n) and fecha_t == fecha_n
    tienda_ok = _tiendas_parecidas(t.get("tienda"), tienda)
    precios_t = _precios(t.get("items"))
    solape = _solape_precios(precios, precios_t)
    solape_ok = min(len(precios), len(precios_t)) >= 3 and solape >= 0.7
    duplicado = (total_ok and (fecha_ok or tienda_ok)) or solape_ok
    return {
        "duplicado": duplicado, "total_ok": total_ok, "fecha_ok": fecha_ok,
        "tienda_ok": tienda_ok, "solape_precios": round(solape, 2),
        "n_precios": (len(precios), len(precios_t)),
    }


def buscar_ticket_similar(user_id: str, tienda, fecha, total, items=None):
    """Detecta el MISMO ticket aunque venga de otra foto (el hash de imagen solo
    detecta archivos idénticos). Es duplicado si:
      - coincide el total (±1 céntimo) y además la fecha o la tienda; o
      - coinciden la mayoría de los precios de los productos (≥70 %, con al
        menos 3 productos), que es lo más estable entre dos fotos distintas.
    Devuelve el ticket guardado o None."""
    try:
        total = float(total)
    except (TypeError, ValueError):
        total = 0.0
    fecha_n = _normalizar_fecha(fecha)
    precios = _precios(items)
    for t in obtener_historial(user_id):
        if _senales_similitud(t, tienda, fecha_n, total, precios)["duplicado"]:
            return t
    return None


def diagnostico_duplicado(user_id: str, tienda, fecha, total, items=None) -> list:
    """Para el panel de admin: por qué cada ticket guardado coincide o no."""
    try:
        total = float(total)
    except (TypeError, ValueError):
        total = 0.0
    fecha_n = _normalizar_fecha(fecha)
    precios = _precios(items)
    filas = []
    for t in obtener_historial(user_id):
        sen = _senales_similitud(t, tienda, fecha_n, total, precios)
        filas.append({
            "guardado": f"{t.get('tienda')} · {t.get('fecha_ticket')} · {t.get('total')}",
            **sen,
        })
    return filas


def registrar_interes_premium(user_id: str, tipo: str) -> None:
    """Guarda un voto de interés. tipo: 'interes_premium:<funcion>' o 'precio_premium:<valor>'."""
    registrar_evento(tipo, user_id)


def obtener_interes_premium() -> dict:
    """Usuarios DISTINTOS que votaron cada función y cada opción de precio."""
    tipos = (
        [f"interes_premium:{k}" for k in FUNCIONES_PREMIUM]
        + [f"precio_premium:{k}" for k in OPCIONES_PRECIO]
    )
    resultado = {t: 0 for t in tipos}
    try:
        usuarios = {t: set() for t in tipos}
        for ev in eventos_col.where(filter=FieldFilter("tipo", "in", tipos)).stream():
            d = ev.to_dict()
            if d.get("user_id") in (None, ""):
                continue
            usuarios[d["tipo"]].add(d["user_id"])
        resultado = {t: len(u) for t, u in usuarios.items()}
    except Exception as e:
        print(f"Error obteniendo interés premium: {e}")
    return resultado


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


def _a_datetime(ts):
    """Convierte un timestamp de evento (datetime o string ISO) a datetime
    con zona horaria. Los eventos antiguos pueden estar en UTC (datetime) o
    sin zona; los nuevos son strings ISO en hora de Madrid."""
    try:
        if not hasattr(ts, "strftime"):
            ts = datetime.fromisoformat(str(ts))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=ZONA_MADRID)
        return ts
    except Exception:
        return None


# Fecha desde la que se mide la "cohorte nueva": usuarios cuyo PRIMER análisis es de
# ese día en adelante. Así se aísla la popularidad entre desconocidos sin borrar
# nada: los testers anteriores quedan fuera de la cohorte y conservan su historial.
# Se puede cambiar con la variable de entorno INICIO_MEDICION (formato AAAA-MM-DD).
INICIO_MEDICION = os.environ.get("INICIO_MEDICION", "2026-11-06")


def _inicio_medicion_dt(fecha_iso: str):
    try:
        return datetime.fromisoformat(f"{fecha_iso}T00:00:00").replace(tzinfo=ZONA_MADRID)
    except ValueError:
        return None


def obtener_metricas(desde: str | None = None) -> dict:
    """Métricas para administración, basadas en el registro de eventos
    'analisis_ok' (no en el contador agregado por usuario) — así se puede
    reconstruir en qué días distintos volvió cada persona, en vez de solo
    saber cuántos análisis hizo en total.

    Si 'desde' (AAAA-MM-DD) se indica, solo cuentan los usuarios de la cohorte
    nueva: los que hicieron su primer análisis a partir de esa fecha."""
    try:
        eventos_analisis = list(
            eventos_col.where(filter=FieldFilter("tipo", "==", "analisis_ok")).stream()
        )

        # 1. Fecha del primer análisis de cada usuario (sobre TODOS los eventos)
        primer_analisis: dict[str, datetime] = {}
        for ev in eventos_analisis:
            d = ev.to_dict()
            uid, dt = d.get("user_id"), _a_datetime(d.get("timestamp"))
            if uid and dt and (uid not in primer_analisis or dt < primer_analisis[uid]):
                primer_analisis[uid] = dt

        # 2. Cohorte: todos, o solo quienes empezaron desde la fecha de inicio
        corte = _inicio_medicion_dt(desde) if desde else None
        if corte:
            cohorte = {u for u, dt in primer_analisis.items() if dt >= corte}
        else:
            cohorte = set(primer_analisis)

        # 3. Días de actividad y último análisis, solo de la cohorte
        dias_por_usuario: dict[str, set] = {}
        ultimo_analisis: dict[str, datetime] = {}
        analisis_ok = 0
        for ev in eventos_analisis:
            d = ev.to_dict()
            uid = d.get("user_id")
            ts = d.get("timestamp")
            if not uid or not ts or uid not in cohorte:
                continue
            analisis_ok += 1
            fecha_str = ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)[:10]
            dias_por_usuario.setdefault(uid, set()).add(fecha_str)
            dt = _a_datetime(ts)
            if dt and (uid not in ultimo_analisis or dt > ultimo_analisis[uid]):
                ultimo_analisis[uid] = dt

        limite_7d = datetime.now(ZONA_MADRID) - timedelta(days=7)
        activos_7d = sum(1 for dt in ultimo_analisis.values() if dt >= limite_7d)

        # En modo cohorte, "usuarios totales" son los de la cohorte; en modo
        # histórico, todos los documentos de usuario (como antes).
        total_usuarios = len(cohorte) if corte else len(list(metricas_col.stream()))
        activados = len(dias_por_usuario)
        volvieron_otro_dia = sum(1 for dias in dias_por_usuario.values() if len(dias) > 1)
        retencion_pct = round(100 * volvieron_otro_dia / activados, 1) if activados else 0.0
        dias_activos_promedio = (
            round(sum(len(d) for d in dias_por_usuario.values()) / activados, 1)
            if activados else 0.0
        )

        todos_tickets = [d.to_dict() for d in tickets_col.stream()]
        if corte:
            todos_tickets = [t for t in todos_tickets if t.get("user_id") in cohorte]
        gastos_manuales = sum(1 for t in todos_tickets if t.get("origen") == "manual")
        total_guardados = len(todos_tickets) - gastos_manuales
        tasa_guardado = round((total_guardados / analisis_ok * 100), 1) if analisis_ok else 0.0

        return {
            "total_usuarios": total_usuarios,
            "activados": activados,
            "total_analisis_ok": analisis_ok,
            "volvieron_otro_dia": volvieron_otro_dia,
            "activos_7d": activos_7d,
            "gastos_manuales": gastos_manuales,
            "retencion_pct": retencion_pct,
            "dias_activos_promedio": dias_activos_promedio,
            "tasa_guardado_pct": tasa_guardado
        }
    except Exception as e:
        print(f"Error obteniendo métricas: {e}")
        return {
            "total_usuarios": 0, "activados": 0, "total_analisis_ok": 0,
            "volvieron_otro_dia": 0, "activos_7d": 0, "gastos_manuales": 0, "retencion_pct": 0, "dias_activos_promedio": 0,
            "tasa_guardado_pct": 0
        }
