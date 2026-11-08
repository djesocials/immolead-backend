import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from jinja2 import Environment, FileSystemLoader, select_autoescape

env = Environment(
    loader=FileSystemLoader(r"C:\immolead-core"),
    autoescape=select_autoescape(["html", "xml"]),
)
html = env.get_template("template.html").render(
    agency_logo_url="http://127.0.0.1:8765/logo.svg",
    property_title="Appartement avec balcon sur cour",
    property_price="845 000 €",
    location="Croix-Rousse, Lyon",
    surface="96",
    rooms="4",
    bedrooms="2",
    description=(
        "Au troisième étage, sur cour.\n\n"
        "Séjour orienté ouest, cuisine séparée, deux chambres, cave et gardien. "
        "Copropriété de 18 lots. Chauffage collectif au gaz."
    ),
    property_image_url="https://images.unsplash.com/photo-1502672260266-1c1ef2d93688?auto=format&fit=crop&w=1400&q=80",
    encrypted_token="preview-token",
    api_endpoint="http://127.0.0.1:8765",
).encode("utf-8")

LOGO = """<svg xmlns="http://www.w3.org/2000/svg" width="160" height="40" viewBox="0 0 160 40">
  <text x="0" y="28" fill="#F8FAFC" font-family="Georgia, serif" font-size="22">Agence</text>
</svg>""".encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(fmt % args, flush=True)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/logo.svg":
            body, ctype = LOGO, "image/svg+xml"
        elif path == "/":
            body, ctype = html, "text/html; charset=utf-8"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        if path != "/api/v1/leads":
            self.send_response(404)
            self.end_headers()
            return
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            data = None
        required = {"token", "full_name", "phone", "email", "financing_status"}
        if not isinstance(data, dict) or not required <= set(data):
            body = '{"message":"Corps incomplet."}'.encode("utf-8")
            status = 422
        else:
            print("LEAD " + json.dumps(data, ensure_ascii=False), flush=True)
            body = b'{"ok":true}'
            status = 201
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8765), Handler).serve_forever()
