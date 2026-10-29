"""
Visita la app con un navegador real (sin interfaz) para que Streamlit Cloud
no la duerme por inactividad. Si ya estaba dormida, pulsa el botón de despertarla.
Falla (y GitHub te avisa por email) si la app no llega a cargar.
"""
import sys
import time

from playwright.sync_api import sync_playwright

URL = "https://app-gastos-gonzalo.streamlit.app"
TEXTO_APP_CARGADA = "Procesador Inteligente de Tickets"   # título de app.py
ESPERA_MAXIMA_S = 150


def app_cargada(page) -> bool:
    # La app de Streamlit Cloud vive dentro de un iframe, por eso se miran todos los frames
    for frame in page.frames:
        try:
            if frame.get_by_text(TEXTO_APP_CARGADA).count() > 0:
                return True
        except Exception:
            pass
    return False


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
        time.sleep(8)

        # Si estaba dormida, aparece el botón "Yes, get this app back up!"
        boton = page.get_by_text("get this app back up")
        if boton.count() > 0:
            print("La app estaba dormida: pulsando el botón para despertarla...")
            boton.first.click()

        inicio = time.time()
        while time.time() - inicio < ESPERA_MAXIMA_S:
            if app_cargada(page):
                time.sleep(10)  # mantener la sesión abierta un momento para que cuente como visita
                print(f"OK: app cargada en {int(time.time() - inicio)} s tras la visita.")
                browser.close()
                return 0
            time.sleep(3)

        print("ERROR: la app no cargó en el tiempo máximo.")
        page.screenshot(path="fallo.png")
        browser.close()
        return 1


if __name__ == "__main__":
    sys.exit(main())
