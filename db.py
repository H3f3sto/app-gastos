"""
Capa de persistencia con TinyDB.
Cuatro tablas:
  - usuarios: 1 doc por user_id, con plan, contador freemium y última actividad
  - tickets:  1 doc por ticket guardado, enlazado por user_id
  - actividad_ip: 1 doc por análisis con éxito, para el rate limit por IP
  - eventos: registro simple de eventos para métricas básicas de uso
"""
from datetime import datetime, timedelta
from tinydb import TinyDB, Query

DB_PATH = "gastos_db.json"
LIMITE_TICKETS_FREE = 15

# Rate limit por IP: protege el cupo compartido de Gemini (5 RPM en el nivel
# gratuito) frente a alguien que rota de UUID/cookie para saltarse el
# freemium, o un bot. No sustituye al contador freemium, lo complementa.
LIMITE_ANALISIS_POR_IP_HORA = 20

db = TinyDB(DB_PATH)
usuarios_table = db.table("usuarios")
tickets_table = db.table("tickets")
actividad_ip_table = db.table("actividad_ip")
eventos_table = db.table("eventos")

Usuario = Query()
Ticket = Query()
ActividadIP = Query()
Evento = Query()


def _mes_actual() -> str:
    return datetime.now().strftime("%Y-%m")


def registrar_evento(tipo: str, user_id: str | None = None) -> None:
    """Registro mínimo de un evento, sin datos personales más allá del
    user_id (un UUID, no un dato identificativo real)."""
    eventos_table.insert({
        "tipo": tipo,
        "timestamp": datetime.now().isoformat(),
        "user_id": user_id,
    })


def obtener_o_crear_usuario(user_id: str) -> dict:
    """Devuelve el doc de usuario, creándolo si es nuevo y reseteando
    el contador si ha cambiado de mes."""
    usuario = usuarios_table.get(Usuario.user_id == user_id)
    mes = _mes_actual()

    if usuario is None:
        usuario = {
            "user_id": user_id,
            "fecha_alta": datetime.now().strftime("%Y-%m-%d"),
            "plan": "free",
            "tickets_mes_actual": 0,
            "mes_contador": mes,
            "ultima_actividad": datetime.now().isoformat(),
        }
        usuarios_table.insert(usuario)
        registrar_evento("usuario_nuevo", user_id)
        return usuario

    if usuario.get("mes_contador") != mes:
        usuarios_table.update(
            {"tickets_mes_actual": 0, "mes_contador": mes},
            Usuario.user_id == user_id,
        )
        usuario["tickets_mes_actual"] = 0
        usuario["mes_contador"] = mes

    return usuario


def puede_escanear(user_id: str) -> bool:
    usuario = obtener_o_crear_usuario(user_id)
    if usuario.get("plan") == "premium":
        return True
    return usuario.get("tickets_mes_actual", 0) < LIMITE_TICKETS_FREE


def tickets_restantes(user_id: str) -> int | str:
    usuario = obtener_o_crear_usuario(user_id)
    if usuario.get("plan") == "premium":
        return "∞ (premium)"
    return max(0, LIMITE_TICKETS_FREE - usuario.get("tickets_mes_actual", 0))


def incrementar_contador(user_id: str) -> None:
    usuario = obtener_o_crear_usuario(user_id)
    usuarios_table.update(
        {
            "tickets_mes_actual": usuario.get("tickets_mes_actual", 0) + 1,
            "ultima_actividad": datetime.now().isoformat(),
        },
        Usuario.user_id == user_id,
    )
    registrar_evento("analisis_ok", user_id)


def guardar_ticket(user_id: str, entrada: dict) -> None:
    """Guarda un ticket en el historial. No descuenta cupo: el cupo se
    descuenta al analizar con Gemini (guardar es gratis, analizar no)."""
    entrada = {**entrada, "user_id": user_id}
    tickets_table.insert(entrada)
    registrar_evento("ticket_guardado", user_id)


def obtener_historial(user_id: str) -> list[dict]:
    tickets = tickets_table.search(Ticket.user_id == user_id)
    resultado = []
    for t in tickets:
        entrada = dict(t)
        entrada["_doc_id"] = t.doc_id  # necesario para poder borrarlo luego
        resultado.append(entrada)
    return sorted(resultado, key=lambda t: t.get("fecha_registro", ""), reverse=True)


def borrar_ticket(user_id: str, doc_id: int) -> bool:
    """Borra un ticket del historial, solo si pertenece a ese user_id
    (para que nadie pueda borrar tickets ajenos aunque adivine un doc_id)."""
    ticket = tickets_table.get(doc_id=doc_id)
    if ticket is None or ticket.get("user_id") != user_id:
        return False
    tickets_table.remove(doc_ids=[doc_id])
    registrar_evento("ticket_borrado", user_id)
    return True


def puede_ip_analizar(ip: str) -> bool:
    """True si esta IP no ha superado el límite de análisis en la última hora."""
    if not ip:
        return True  # sin IP detectada (p. ej. local), no bloqueamos
    hace_una_hora = (datetime.now() - timedelta(hours=1)).isoformat()
    llamadas_recientes = actividad_ip_table.count(
        (ActividadIP.ip == ip) & (ActividadIP.timestamp >= hace_una_hora)
    )
    return llamadas_recientes < LIMITE_ANALISIS_POR_IP_HORA


def registrar_analisis_ip(ip: str) -> None:
    """Registra un análisis con éxito para esta IP y limpia registros viejos."""
    if not ip:
        return
    actividad_ip_table.insert({"ip": ip, "timestamp": datetime.now().isoformat()})
    # Limpieza: borra registros de más de 24h para que la tabla no crezca sin límite
    hace_un_dia = (datetime.now() - timedelta(hours=24)).isoformat()
    actividad_ip_table.remove(ActividadIP.timestamp < hace_un_dia)


def obtener_metricas() -> dict:
    """Métricas agregadas y anónimas para el panel de admin."""
    ahora = datetime.now()
    hace_7d = (ahora - timedelta(days=7)).isoformat()
    hace_30d = (ahora - timedelta(days=30)).isoformat()

    todos_usuarios = usuarios_table.all()
    activos_7d = sum(1 for u in todos_usuarios if u.get("ultima_actividad", "") >= hace_7d)
    activos_30d = sum(1 for u in todos_usuarios if u.get("ultima_actividad", "") >= hace_30d)

    todos_eventos = eventos_table.all()
    analisis_ok = sum(1 for e in todos_eventos if e["tipo"] == "analisis_ok")
    tickets_guardados = sum(1 for e in todos_eventos if e["tipo"] == "ticket_guardado")

    return {
        "total_usuarios": len(todos_usuarios),
        "usuarios_activos_7d": activos_7d,
        "usuarios_activos_30d": activos_30d,
        "total_analisis_ok": analisis_ok,
        "total_tickets_guardados": tickets_guardados,
        "tasa_guardado_pct": round(100 * tickets_guardados / analisis_ok, 1) if analisis_ok else 0,
    }