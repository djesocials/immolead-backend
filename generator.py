#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import qrcode
from jinja2 import (
    Environment,
    FileSystemLoader,
    StrictUndefined,
    TemplateNotFound,
    select_autoescape,
)
from PIL import ImageOps
from qrcode.constants import ERROR_CORRECT_Q

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_NAME = "template.html"
JWT_ALGORITHM = "HS256"

NAVY = "#0F172A"
GOLD = "#D4AF37"
WHITE = "#FFFFFF"

QUIET_ZONE_MODULES = 4
FRAME_MODULES = 2

SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
REQUIRED_PROPERTY_KEYS = (
    "ref", "title", "price", "location", "surface", "rooms", "bedrooms", "description", "image_url",
)

AGENCIES: dict[str, dict] = {
    "maison-lumiere": {
        "name": "Maison Lumière Immobilier",
        "email": "contact@agence-demo.fr",
        "logo_url": "https://placehold.co/280x80/0F172A/D4AF37?text=Maison+Lumi%C3%A8re",
        "sheet_id": "",
        "properties": [
            {
                "ref": "ML-2026-014",
                "title": "Villa d'architecte avec piscine",
                "price": 1250000,
                "location": "Bordeaux, Caudéran",
                "surface": 210,
                "rooms": 7,
                "bedrooms": 4,
                "description": (
                    "Au cœur d'un quartier résidentiel recherché, cette villa contemporaine "
                    "offre de beaux volumes baignés de lumière.\n\n"
                    "Séjour cathédrale, cuisine ouverte, suite parentale avec dressing, "
                    "jardin paysager et piscine chauffée."
                ),
                "image_url": "https://images.unsplash.com/photo-1600596542815-ffad4c1539a9?auto=format&fit=crop&w=1400&q=80",
            },
            {
                "ref": "ML-2026-021",
                "title": "Appartement haussmannien rénové",
                "price": 685000,
                "location": "Bordeaux, Chartrons",
                "surface": 112,
                "rooms": 5,
                "bedrooms": 3,
                "description": (
                    "Au deuxième étage d'un immeuble en pierre de taille, appartement traversant "
                    "conservant parquets, moulures et cheminées.\n\n"
                    "Cuisine équipée, deux salles de bains, cave et local à vélos."
                ),
                "image_url": "https://images.unsplash.com/photo-1600585154340-be6161a56a0c?auto=format&fit=crop&w=1400&q=80",
            },
        ],
    },
    "domaine-azur": {
        "name": "Domaine Azur Prestige",
        "email": "agents@agence-demo.fr",
        "logo_url": "https://placehold.co/280x80/0F172A/D4AF37?text=Domaine+Azur",
        "sheet_id": "",
        "properties": [
            {
                "ref": "DA-2026-007",
                "title": "Mas provençal en pierre, vue dégagée",
                "price": 960000,
                "location": "Aix-en-Provence",
                "surface": 245,
                "rooms": 8,
                "bedrooms": 5,
                "description": (
                    "Mas restauré avec goût sur un terrain de 6 000 m² planté d'oliviers.\n\n"
                    "Grande pièce de vie avec cheminée, dépendances aménageables, piscine et "
                    "terrasses ombragées."
                ),
                "image_url": "https://images.unsplash.com/photo-1613490493576-7fde63acd811?auto=format&fit=crop&w=1400&q=80",
            },
        ],
    },
}

def format_price(price: int | float | str) -> str:
    if isinstance(price, str):
        return price
    return f"{int(price):,}".replace(",", "\u202f") + "\u00a0€"

def validate_catalogue(catalogue: dict[str, dict]) -> None:
    seen_refs: dict[str, str] = {}
    for slug, agency in catalogue.items():
        if not SLUG_RE.fullmatch(slug):
            raise ValueError(f"Invalid agency slug {slug!r}")
        if not agency.get("email"):
            raise ValueError(f"Agency {slug!r} has no 'email'.")
        for prop in agency.get("properties", []):
            missing = [k for k in REQUIRED_PROPERTY_KEYS if k not in prop]
            if missing:
                raise ValueError(f"Property in {slug!r} missing: {', '.join(missing)}")
            ref = str(prop["ref"])
            if not REF_RE.fullmatch(ref):
                raise ValueError(f"Invalid property ref {ref!r}")
            if ref in seen_refs:
                raise ValueError(f"Duplicate property ref {ref!r}")
            seen_refs[ref] = slug

def issue_routing_token(
    secret_key: str,
    agency_email: str,
    agency_slug: str,
    property_ref: str,
    sheet_id: str | None = None,
    ttl_days: int | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    claims: dict = {
        "agency_email": agency_email,
        "agency_slug": agency_slug,
        "property_ref": property_ref,
        "iat": now,
    }
    if sheet_id:
        claims["sheet_id"] = sheet_id
    if ttl_days:
        claims["exp"] = now + timedelta(days=ttl_days)
    return jwt.encode(claims, secret_key, algorithm=JWT_ALGORITHM)

def build_qr(url: str, box_size: int) -> qrcode.QRCode:
    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_Q,
        box_size=box_size,
        border=QUIET_ZONE_MODULES,
    )
    qr.add_data(url)
    qr.make(fit=True)
    return qr

def save_qr_png(qr: qrcode.QRCode, path: Path, background: str) -> None:
    gold_bg = background == "gold"
    image = qr.make_image(fill_color=NAVY, back_color=GOLD if gold_bg else WHITE)
    pil = image.get_image() if hasattr(image, "get_image") else image._img
    if not gold_bg:
        pil = ImageOps.expand(pil, border=FRAME_MODULES * qr.box_size, fill=GOLD)
    pil.save(path, format="PNG", dpi=(300, 300))

def save_qr_svg(qr: qrcode.QRCode, path: Path, background: str, title: str) -> None:
    matrix = qr.get_matrix()
    gold_bg = background == "gold"
    frame = 0 if gold_bg else FRAME_MODULES
    size = len(matrix) + 2 * frame

    segments: list[str] = []
    for y, row in enumerate(matrix):
        x = 0
        while x < len(row):
            if row[x]:
                start = x
                while x < len(row) and row[x]:
                    x += 1
                run = x - start
                segments.append(f"M{start + frame} {y + frame}h{run}v1h-{run}z")
            else:
                x += 1

    layers = []
    if not gold_bg:
        layers.append(f'<rect width="{size}" height="{size}" fill="{GOLD}"/>')
        layers.append(
            f'<rect x="{frame}" y="{frame}" width="{size - 2 * frame}" '
            f'height="{size - 2 * frame}" fill="{WHITE}"/>'
        )
    else:
        layers.append(f'<rect width="{size}" height="{size}" fill="{GOLD}"/>')

    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
        f'width="{size * 10}" height="{size * 10}" shape-rendering="crispEdges" role="img">'
        f"<title>{html.escape(title)}</title>"
        + "".join(layers)
        + f'<path fill="{NAVY}" d="{"".join(segments)}"/></svg>\n'
    )
    path.write_text(svg, encoding="utf-8")

def build(
    output_dir: Path,
    base_url: str,
    api_endpoint: str,
    secret_key: str,
    ttl_days: int | None,
    qr_background: str,
    box_size: int,
    only_agency: str | None,
) -> int:
    validate_catalogue(AGENCIES)

    selected = {k: v for k, v in AGENCIES.items() if only_agency in (None, k)}
    if only_agency and not selected:
        raise ValueError(f"Unknown agency {only_agency!r}")

    env = Environment(
        loader=FileSystemLoader(BASE_DIR),
        autoescape=select_autoescape(["html"]),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    template = env.get_template(TEMPLATE_NAME)

    qr_dir = output_dir / "qrcodes"
    qr_dir.mkdir(parents=True, exist_ok=True)

    count = 0
    for slug, agency in selected.items():
        page_dir = output_dir / "agencies" / slug
        page_dir.mkdir(parents=True, exist_ok=True)

        for prop in agency["properties"]:
            ref = str(prop["ref"])

            token = issue_routing_token(
                secret_key=secret_key,
                agency_email=agency["email"],
                agency_slug=slug,
                property_ref=ref,
                sheet_id=agency.get("sheet_id") or None,
                ttl_days=ttl_days,
            )

            page = template.render(
                agency_logo_url=agency.get("logo_url", ""),
                property_title=prop["title"],
                property_price=format_price(prop["price"]),
                location=prop["location"],
                surface=prop["surface"],
                rooms=prop["rooms"],
                bedrooms=prop["bedrooms"],
                description=prop["description"],
                property_image_url=prop["image_url"],
                encrypted_token=token,
                api_endpoint=api_endpoint,
            )
            page_path = page_dir / f"{ref}.html"
            page_path.write_text(page, encoding="utf-8")

            public_url = f"{base_url}/agencies/{slug}/{ref}.html"
            qr = build_qr(public_url, box_size)
            save_qr_png(qr, qr_dir / f"{ref}_qr.png", qr_background)
            save_qr_svg(qr, qr_dir / f"{ref}_qr.svg", qr_background, f"QR code {ref}")

            count += 1
            print(f"  OK  {slug}/{ref}  ->  {public_url}")

    return count

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", default=os.getenv("PUBLIC_BASE_URL", "https://immolead-static.vercel.app"))
    p.add_argument("--api-endpoint", default=os.getenv("API_ENDPOINT", "https://immolead-backend.onrender.com"))
    p.add_argument("--output", default=str(BASE_DIR / "dist"))
    p.add_argument("--agency", default=None)
    p.add_argument("--qr-background", choices=("white", "gold"), default="white")
    p.add_argument("--box-size", type=int, default=30)
    p.add_argument("--token-ttl-days", type=int, default=int(os.getenv("TOKEN_TTL_DAYS", "0")) or None)
    return p.parse_args()

def main() -> int:
    args = parse_args()
    secret_key = os.getenv("SECRET_KEY", "super_secure_immolead_production_secret_key_2026_xyz")

    try:
        print(f"Building into {args.output}")
        count = build(
            output_dir=Path(args.output),
            base_url=args.base_url,
            api_endpoint=args.api_endpoint,
            secret_key=secret_key,
            ttl_days=args.token_ttl_days,
            qr_background=args.qr_background,
            box_size=args.box_size,
            only_agency=args.agency,
        )
    except TemplateNotFound:
        print(f"Error: {TEMPLATE_NAME} not found.", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Done: {count} page(s) and QR code(s) generated successfully!")
    return 0

if __name__ == "__main__":
    sys.exit(main())