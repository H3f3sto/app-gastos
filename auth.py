"""
Identificación de usuario sin registro: UUID generado una vez y
guardado en una cookie cifrada del navegador (streamlit-cookies-manager).
Requiere: pip install streamlit-cookies-manager
"""
import logging
import os
import uuid
import streamlit as st
 
# El paquete streamlit-cookies-manager usa el decorador antiguo `st.cache`,
# eliminado en versiones recientes de Streamlit. Este parche de compatibilidad
# lo redirige a `st.cache_resource` antes de importar la librería.
if not hasattr(st, "cache"):
    st.cache = st.cache_resource
 
from streamlit_cookies_manager import EncryptedCookieManager
 
# Valor de ejemplo que figuraba en el código (público en GitHub). Nunca debe usarse
# de verdad: quien lo conozca podría fabricar cookies válidas.
_PASSWORD_POR_DEFECTO = "cambia-esta-clave-en-produccion"
_LONGITUD_RECOMENDADA = 16
 
 
def _leer_cookie_password() -> str | None:
    """Lee COOKIE_PASSWORD del entorno (.env en local, Secrets en Streamlit Cloud).
    Si no está como variable, se intenta leer directamente de st.secrets."""
    valor = os.environ.get("COOKIE_PASSWORD")
    if not valor:
        try:
            valor = st.secrets.get("COOKIE_PASSWORD")
        except Exception:
            valor = None
    return valor or None
 
 
def get_cookie_manager() -> EncryptedCookieManager:
    password = _leer_cookie_password()
 
    # La app se niega a arrancar sin una contraseña propia: un fallo de configuración
    # debe notarse al instante, no quedarse en silencio con una clave pública.
    if not password or password == _PASSWORD_POR_DEFECTO:
        st.error(
            "⚠️ Configuración de seguridad incompleta: falta definir `COOKIE_PASSWORD` "
            "(o sigue usando el valor de ejemplo). La app no puede arrancar así."
        )
        st.caption(
            "Administrador: añade `COOKIE_PASSWORD = \"<cadena larga y aleatoria>\"` en los "
            "Secrets de Streamlit Cloud (encima del bloque [firebase]) o en tu archivo .env."
        )
        st.stop()
 
    if len(password) < _LONGITUD_RECOMENDADA:
        # Solo al registro del servidor: los usuarios no deben ver este mensaje.
        logging.warning(
            "COOKIE_PASSWORD tiene menos de %d caracteres; se recomienda una más larga.",
            _LONGITUD_RECOMENDADA,
        )
 
    cookies = EncryptedCookieManager(prefix="gastos_app/", password=password)
    if not cookies.ready():
        # El componente necesita un rerun para inicializarse en el navegador
        st.stop()
    return cookies
 
 
def obtener_user_id(cookies: EncryptedCookieManager) -> str:
    user_id = cookies.get("user_id")
    if not user_id:
        user_id = str(uuid.uuid4())
        cookies["user_id"] = user_id
        cookies.save()
    return user_id
 
 
def introducir_codigo_manual(cookies: EncryptedCookieManager) -> None:
    """Fallback: permite al usuario pegar un código existente
    (por si cambia de navegador o borra cookies)."""
    with st.expander("¿Ya tienes un código de otra sesión?"):
        codigo = st.text_input("Pega tu código aquí", key="codigo_manual")
        if st.button("Usar este código"):
            if codigo.strip():
                cookies["user_id"] = codigo.strip()
                cookies.save()
                st.rerun()
 