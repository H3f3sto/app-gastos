"""
Identificación de usuario sin registro: UUID generado una vez y
guardado en una cookie cifrada del navegador (streamlit-cookies-manager).
Requiere: pip install streamlit-cookies-manager
"""
import os
import uuid
import streamlit as st

# El paquete streamlit-cookies-manager usa el decorador antiguo `st.cache`,
# eliminado en versiones recientes de Streamlit. Este parche de compatibilidad
# lo redirige a `st.cache_resource` antes de importar la librería.
if not hasattr(st, "cache"):
    st.cache = st.cache_resource

from streamlit_cookies_manager import EncryptedCookieManager

# Clave para cifrar la cookie. En producción, ponla en variable de entorno.
COOKIE_PASSWORD = os.environ.get("COOKIE_PASSWORD", "cambia-esta-clave-en-produccion")


def get_cookie_manager() -> EncryptedCookieManager:
    cookies = EncryptedCookieManager(prefix="gastos_app/", password=COOKIE_PASSWORD)
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