import json
import os
from pathlib import Path
import streamlit as st

DATA_FILE = Path("agencies.json")

def load_data():
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return {}

def save_data(data):
    DATA_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

def trigger_generator():
    """Helper to automatically trigger static generation after changes."""
    os.environ["SECRET_KEY"] = "super_secure_immolead_production_secret_key_2026_xyz"
    os.environ["PUBLIC_BASE_URL"] = "https://immolead-static.vercel.app"
    os.environ["API_ENDPOINT"] = "https://immolead-backend.onrender.com"
    
    import generator
    generator.AGENCIES = load_data()
    return generator.build(
        output_dir=Path("dist"),
        base_url=os.environ["PUBLIC_BASE_URL"],
        api_endpoint=os.environ["API_ENDPOINT"],
        secret_key=os.environ["SECRET_KEY"],
        ttl_days=None,
        qr_background="white",
        box_size=30,
        only_agency=None
    )

st.set_page_config(page_title="ImmoLead Manager", page_icon="🏡", layout="centered")

st.title("🏡 ImmoLead Agency & Property Manager")
st.write("Gérez vos agences, ajoutez, modifiez ou supprimez vos biens et générez vos pages instantanément.")

data = load_data()

# --- Sidebar: Select, Create, or Delete Agency ---
st.sidebar.header("Gestion des Agences")
agency_slugs = list(data.keys())
selected_agency = st.sidebar.selectbox("Choisir une agence", ["+ Nouvelle agence"] + agency_slugs)

if selected_agency == "+ Nouvelle agence":
    st.subheader("Créer une nouvelle agence")
    new_slug_raw = st.text_input("Identifiant URL (ex: agence-cahors)")
    new_slug = new_slug_raw.strip().lower().replace(" ", "-")
    if new_slug != new_slug_raw.strip().lower() and new_slug_raw:
        st.info(f"💡 Identifiant converti automatiquement en : `{new_slug}`")
        
    new_name = st.text_input("Nom complet de l'agence")
    new_email = st.text_input("Email de contact pour les leads")
    new_logo = st.text_input("URL du Logo (optionnel)")
    new_sheet = st.text_input("Google Sheet ID (laisser vide pour défaut)")
    
    if st.button("Créer l'agence"):
        if new_slug and new_email:
            data[new_slug] = {
                "name": new_name,
                "email": new_email,
                "logo_url": new_logo,
                "sheet_id": new_sheet,
                "properties": []
            }
            save_data(data)
            st.success(f"Agence {new_name} créée avec succès !")
            st.rerun()
        else:
            st.error("Veuillez remplir au moins l'identifiant et l'email.")
    st.stop()

# --- Main Form: Agency Selected ---
agency = data[selected_agency]
st.header(f"Agence : {agency['name']}")

# --- Agency Settings Expander (Edit / Delete Agency) ---
with st.expander("⚙️ Modifier ou supprimer cette agence"):
    with st.form("edit_agency_form"):
        edit_name = st.text_input("Nom complet", value=agency["name"])
        edit_email = st.text_input("Email de contact", value=agency["email"])
        edit_logo = st.text_input("URL du Logo", value=agency.get("logo_url", ""))
        edit_sheet = st.text_input("Google Sheet ID", value=agency.get("sheet_id", ""))
        
        update_agency_btn = st.form_submit_button("Enregistrer les modifications de l'agence")
        if update_agency_btn:
            agency["name"] = edit_name
            agency["email"] = edit_email
            agency["logo_url"] = edit_logo
            agency["sheet_id"] = edit_sheet
            save_data(data)
            st.success("Informations de l'agence mises à jour !")
            st.rerun()

    if st.button("🗑️ Supprimer cette agence et tous ses biens", type="secondary"):
        del data[selected_agency]
        save_data(data)
        st.warning("Agence supprimée.")
        st.rerun()

st.divider()

# --- Add Property Form ---
with st.form("property_form"):
    st.subheader("📋 Ajouter un nouveau bien")
    
    col1, col2 = st.columns(2)
    with col1:
        prop_ref = st.text_input("Référence du bien (ex: CAH-2026-01)")
        prop_title = st.text_input("Titre de l'annonce (ex: Maison de village avec vue)")
        prop_price = st.number_input("Prix (€)", min_value=0, value=250000, step=5000)
        prop_location = st.text_input("Localisation (ex: Cahors, Lot)")
        
    with col2:
        prop_surface = st.number_input("Surface habitable (m²)", min_value=0, value=120)
        prop_rooms = st.number_input("Nombre total de pièces", min_value=1, value=4)
        prop_bedrooms = st.number_input("Nombre de chambres", min_value=0, value=3)

    st.subheader("📝 Description et Visuels")
    prop_desc = st.text_area(
        "Description détaillée", 
        value="Charmante propriété offrant de beaux volumes, lumineuse et idéalement située..."
    )
    
    uploaded_image = st.file_uploader("📸 Photo principale du bien", type=["jpg", "jpeg", "png", "webp"])
    
    submitted = st.form_submit_button("🚀 Enregistrer et Générer la Page & QR Code")
    
    if submitted:
        if prop_ref and prop_title:
            image_url = "https://images.unsplash.com/photo-1564013799919-ab600027ffc6?auto=format&fit=crop&w=1400&q=80"
            if uploaded_image is not None:
                img_dir = Path("dist/images")
                img_dir.mkdir(parents=True, exist_ok=True)
                img_path = img_dir / uploaded_image.name
                img_path.write_bytes(uploaded_image.getbuffer())
                image_url = f"../../images/{uploaded_image.name}"

            new_prop = {
                "ref": prop_ref,
                "title": prop_title,
                "price": prop_price,
                "location": prop_location,
                "surface": prop_surface,
                "rooms": prop_rooms,
                "bedrooms": prop_bedrooms,
                "description": prop_desc,
                "image_url": image_url
            }
            agency["properties"].append(new_prop)
            save_data(data)
            
            count = trigger_generator()
            st.success(f"Bien enregistré avec succès ! {count} page(s) et QR code(s) générés dans le dossier dist/ 🎉")
        else:
            st.error("Veuillez renseigner au moins la référence et le titre du bien.")

st.divider()
st.markdown("### 📁 Biens actuellement enregistrés")

if not agency["properties"]:
    st.info("Aucun bien enregistré pour le moment dans cette agence.")

for idx, p in enumerate(agency["properties"]):
    with st.container():
        st.write(f"**{p['ref']}** : {p['title']} — *{p['price']:,} €* | {p['surface']} m² | {p['rooms']} pièces ({p['location']})")
        
        # Action buttons / Expanders for editing or deleting individual properties
        col_edit, col_del = st.columns(2)
        with col_edit:
            with st.expander(f"✏️ Modifier ce bien ({p['ref']})"):
                with st.form(key=f"edit_prop_form_{idx}"):
                    e_ref = st.text_input("Référence", value=p["ref"])
                    e_title = st.text_input("Titre", value=p["title"])
                    e_price = st.number_input("Prix (€)", min_value=0, value=int(p["price"]), step=5000)
                    e_location = st.text_input("Localisation", value=p["location"])
                    e_surface = st.number_input("Surface (m²)", min_value=0, value=int(p["surface"]))
                    e_rooms = st.number_input("Pièces", min_value=1, value=int(p["rooms"]))
                    e_bedrooms = st.number_input("Chambres", min_value=0, value=int(p["bedrooms"]))
                    e_desc = st.text_area("Description", value=p["description"])
                    
                    save_edit_btn = st.form_submit_button("Mettre à jour le bien")
                    if save_edit_btn:
                        agency["properties"][idx] = {
                            "ref": e_ref,
                            "title": e_title,
                            "price": e_price,
                            "location": e_location,
                            "surface": e_surface,
                            "rooms": e_rooms,
                            "bedrooms": e_bedrooms,
                            "description": e_desc,
                            "image_url": p.get("image_url", "https://images.unsplash.com/photo-1564013799919-ab600027ffc6?auto=format&fit=crop&w=1400&q=80")
                        }
                        save_data(data)
                        trigger_generator()
                        st.success("Bien mis à jour et pages régénérées avec succès !")
                        st.rerun()
                        
        with col_del:
            if st.button("🗑️ Supprimer ce bien", key=f"del_prop_{idx}"):
                agency["properties"].pop(idx)
                save_data(data)
                trigger_generator()
                st.success(f"Bien {p['ref']} supprimé.")
                st.rerun()
        
        st.divider()