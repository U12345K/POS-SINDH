# app.py
# Streamlit Water POS — Branded for PURE SINDH WATER LTD
# Features in this file:
# - Email sign-up / login (hashed)
# - Billing with default products (500ML PACK, 1.5LTR PACK, 6LTR BOTTLE)
# - Quantity & Price fields (no optional item)
# - Advance balance auto-applies to next bill; remaining tracked
# - PDF invoice (A4, centered, blue header row, date/time, footer note)
# - Credit ledger, History, Admin (Delete All Business Data)
# - Persistent storage using SQLite

import os
import io
import secrets
import hashlib
import hmac
from datetime import datetime
import sqlite3

import pandas as pd
import streamlit as st
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.lib import colors

DB_PATH = "water_pos_puresindh.db"
COMPANY_NAME = "PURE SINDH WATER LTD"
FOOTER_NOTE = "Thank you for choosing PURE SINDH WATER LTD"
THEME_ACCENT = colors.Color(red=0/255, green=110/255, blue=150/255)  # teal/blue

# -----------------------------
# Database helpers
# -----------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS ledger (
            customer_id INTEGER PRIMARY KEY,
            advance REAL NOT NULL DEFAULT 0,
            remaining REAL NOT NULL DEFAULT 0,
            FOREIGN KEY(customer_id) REFERENCES customers(id)
        );
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS bills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            bill_date TEXT NOT NULL,
            subtotal REAL NOT NULL,
            advance_applied REAL NOT NULL DEFAULT 0,
            amount_paid REAL NOT NULL DEFAULT 0,
            remaining_after REAL NOT NULL DEFAULT 0,
            advance_after REAL NOT NULL DEFAULT 0,
            FOREIGN KEY(customer_id) REFERENCES customers(id)
        );
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS bill_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bill_id INTEGER NOT NULL,
            item_name TEXT NOT NULL,
            quantity REAL NOT NULL,
            unit_price REAL NOT NULL,
            line_total REAL NOT NULL,
            FOREIGN KEY(bill_id) REFERENCES bills(id)
        );
        """
    )

    conn.commit()
    conn.close()


# -----------------------------
# Security helpers
# -----------------------------

def hash_password(password: str, salt: str) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 150_000)
    return dk.hex()


def create_user(email: str, password: str) -> tuple[bool, str]:
    conn = get_conn()
    cur = conn.cursor()
    salt = secrets.token_hex(16)
    pw_hash = hash_password(password, salt)
    try:
        cur.execute(
            "INSERT INTO users(email, password_hash, salt, created_at) VALUES (?,?,?,?)",
            (email.lower().strip(), pw_hash, salt, datetime.utcnow().isoformat()),
        )
        conn.commit()
        return True, ""
    except sqlite3.IntegrityError:
        return False, "Email already exists"
    finally:
        conn.close()


def verify_user(email: str, password: str) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT password_hash, salt FROM users WHERE email=?", (email.lower().strip(),))
    row = cur.fetchone()
    conn.close()
    if not row:
        return False
    calc = hash_password(password, row["salt"])
    return hmac.compare_digest(calc, row["password_hash"])


# -----------------------------
# Customer & Ledger helpers
# -----------------------------

def get_or_create_customer(name: str) -> int:
    name = name.strip()
    if not name:
        raise ValueError("Customer name required")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM customers WHERE name=?", (name,))
    row = cur.fetchone()
    if row:
        cid = row["id"]
    else:
        cur.execute("INSERT INTO customers(name, created_at) VALUES (?, ?)", (name, datetime.utcnow().isoformat()))
        cid = cur.lastrowid
        cur.execute("INSERT OR IGNORE INTO ledger(customer_id, advance, remaining) VALUES (?,0,0)", (cid,))
        conn.commit()
    conn.close()
    return cid


def get_ledger(customer_id: int) -> tuple[float, float]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT advance, remaining FROM ledger WHERE customer_id=?", (customer_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        return float(row["advance"]), float(row["remaining"])
    return 0.0, 0.0


def update_ledger(customer_id: int, new_advance: float, new_remaining: float) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE ledger SET advance=?, remaining=? WHERE customer_id=?", (round(new_advance, 2), round(new_remaining, 2), customer_id))
    conn.commit()
    conn.close()


# -----------------------------
# Billing logic
# -----------------------------

def create_bill(customer_name: str, items: list[dict], amount_paid: float) -> tuple[int, dict, bytes]:
    """
    items: list of {name, qty, price}
    amount_paid: payment made now

    Behavior:
    - Apply existing advance to subtotal first
    - Then apply amount_paid
    - Any leftover payment becomes new advance
    - Remaining debt is tracked in ledger
    """
    customer_id = get_or_create_customer(customer_name)
    adv_before, rem_before = get_ledger(customer_id)

    subtotal = 0.0
    lines = []
    for it in items:
        q = float(it.get("qty", 0))
        p = float(it.get("price", 0))
        if q <= 0 or p < 0:
            continue
        lt = round(q * p, 2)
        subtotal += lt
        lines.append({"item": it.get("name"), "qty": q, "price": p, "total": lt})

    subtotal = round(subtotal, 2)

    # Use advance first
    advance_applied = min(adv_before, subtotal)
    after_advance = subtotal - advance_applied

    payment = max(0.0, float(amount_paid))
    after_payment = after_advance - payment

    if after_payment > 0:
        # Customer still owes
        new_remaining = rem_before + after_payment
        new_advance = adv_before - advance_applied
    else:
        # Overpaid: becomes new advance
        overpay = abs(after_payment)
        new_advance = (adv_before - advance_applied) + overpay
        new_remaining = rem_before

    new_advance = round(new_advance, 2)
    new_remaining = round(new_remaining, 2)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO bills(customer_id, bill_date, subtotal, advance_applied, amount_paid, remaining_after, advance_after) VALUES (?,?,?,?,?,?,?)",
        (customer_id, datetime.now().isoformat(), subtotal, advance_applied, payment, new_remaining, new_advance),
    )
    bill_id = cur.lastrowid

    for ln in lines:
        cur.execute(
            "INSERT INTO bill_items(bill_id, item_name, quantity, unit_price, line_total) VALUES (?,?,?,?,?)",
            (bill_id, ln["item"], ln["qty"], ln["price"], ln["total"]),
        )

    conn.commit()
    conn.close()

    update_ledger(customer_id, new_advance, new_remaining)

    pdf_bytes = build_invoice_pdf(
        serial=bill_id,
        customer_name=customer_name,
        bill_date=datetime.now(),
        items=lines,
        subtotal=subtotal,
        advance_applied=advance_applied,
        amount_paid=payment,
        advance_after=new_advance,
        remaining_after=new_remaining,
    )

    return bill_id, {"subtotal": subtotal, "advance_applied": advance_applied, "amount_paid": payment, "advance_after": new_advance, "remaining_after": new_remaining}, pdf_bytes


# -----------------------------
# PDF generation (A4, centered, blue header)
# -----------------------------

def build_invoice_pdf(serial: int, customer_name: str, bill_date: datetime, items: list, subtotal: float, advance_applied: float, amount_paid: float, advance_after: float, remaining_after: float) -> bytes:
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    margin = 18 * mm
    usable_w = width - 2 * margin
    center_x = width / 2

    # Header (left company, right date/time)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(margin, height - margin, COMPANY_NAME)
    c.setFont("Helvetica", 10)
    c.drawRightString(width - margin, height - margin + 4, bill_date.strftime("%Y-%m-%d %H:%M"))

    y = height - margin - 14
    c.line(margin, y, width - margin, y)

    # Bill meta
    y -= 18
    c.setFont("Helvetica", 11)
    c.drawString(margin, y, f"Bill Serial: {serial}")
    c.drawString(margin + 220, y, f"Customer: {customer_name}")

    # Table header (blue bar)
    y -= 22
    c.setFillColor(THEME_ACCENT)
    c.rect(margin, y - 6, usable_w, 18, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(margin + 6, y, "Item")
    c.drawRightString(margin + usable_w * 0.6, y, "Quantity")
    c.drawRightString(margin + usable_w * 0.8, y, "Rate")
    c.drawRightString(width - margin - 6, y, "Total")

    # Items
    y -= 18
    c.setFillColor(colors.black)
    c.setFont("Helvetica", 10)
    for it in items:
        if y < 60 * mm:
            c.showPage()
            y = height - margin - 20
        c.drawString(margin + 6, y, str(it.get("item")))
        c.drawRightString(margin + usable_w * 0.6, y, f"{it.get('qty'):.2f}")
        c.drawRightString(margin + usable_w * 0.8, y, f"{it.get('price'):.2f}")
        c.drawRightString(width - margin - 6, y, f"{it.get('total'):.2f}")
        y -= 12

    # Summary
    y -= 8
    c.setFont("Helvetica", 11)
    c.drawRightString(margin + usable_w * 0.8, y, "Subtotal:")
    c.drawRightString(width - margin - 6, y, f"{subtotal:.2f}")
    y -= 14
    c.drawRightString(margin + usable_w * 0.8, y, "Advance Applied:")
    c.drawRightString(width - margin - 6, y, f"-{advance_applied:.2f}")
    y -= 14
    c.drawRightString(margin + usable_w * 0.8, y, "Amount Paid (Now):")
    c.drawRightString(width - margin - 6, y, f"-{amount_paid:.2f}")
    y -= 14
    c.setFont("Helvetica-Bold", 12)
    c.drawRightString(margin + usable_w * 0.8, y, "Remaining After:")
    c.drawRightString(width - margin - 6, y, f"{remaining_after:.2f}")
    y -= 16
    c.drawRightString(margin + usable_w * 0.8, y, "Advance Balance:")
    c.drawRightString(width - margin - 6, y, f"{advance_after:.2f}")

    # Footer note centered
    c.setFont("Helvetica-Oblique", 10)
    c.setFillColor(colors.grey)
    c.drawCentredString(center_x, 18 * mm, FOOTER_NOTE)

    c.showPage()
    c.save()
    pdf = buffer.getvalue()
    buffer.close()
    return pdf


# -----------------------------
# UI: styling helpers
# -----------------------------

def inject_css():
    st.markdown(
        """
        <style>
        .sidebar .sidebar-content {background: linear-gradient(180deg,#e6f7fb,#ffffff);}
        .block-container{padding-top:1rem;padding-bottom:3rem}
        .card{background:#ffffff;border-radius:10px;padding:14px;margin-bottom:10px;box-shadow:0 1px 4px rgba(0,0,0,0.06)}
        .accent{color:#06758f;font-weight:700}
        </style>
        """,
        unsafe_allow_html=True,
    )


# -----------------------------
# Pages
# -----------------------------

def page_auth():
    st.title(f"💧 {COMPANY_NAME} — Login / Sign up")
    tab1, tab2 = st.tabs(["Login", "Sign up"])

    with tab1:
        with st.form("login_form"):
            email = st.text_input("Email", key="login_email")
            password = st.text_input("Password", type="password", key="login_pw")
            submit = st.form_submit_button("Login")
        if submit:
            if verify_user(email, password):
                st.session_state["user"] = email
                st.success("Logged in successfully")
                st.rerun()
            else:
                st.error("Invalid credentials")

    with tab2:
        with st.form("signup_form"):
            email2 = st.text_input("Email", key="signup_email")
            pw1 = st.text_input("Password", type="password", key="signup_pw1")
            pw2 = st.text_input("Confirm Password", type="password", key="signup_pw2")
            s = st.form_submit_button("Create account")
        if s:
            if not email2 or not pw1:
                st.warning("Email and password are required")
            elif pw1 != pw2:
                st.warning("Passwords do not match")
            else:
                ok, err = create_user(email2, pw1)
                if ok:
                    st.success("Account created. Please log in.")
                else:
                    st.error(err or "Could not create user")


def billing_form():
    st.subheader("🧾 Create Bill")
    st.caption("Enter quantities and per-unit prices. Quantity = number of units; Price = per-unit rate.")

    with st.form("bill_form", clear_on_submit=False):
        colL, colR = st.columns([2, 1])
        with colL:
            customer_name = st.text_input("Customer Name", key="cust_name")
        with colR:
            amount_paid = st.number_input("Amount Paid (this bill)", min_value=0.0, value=0.0, step=0.5, key="amount_paid")

        st.markdown("**Items (Product — Quantity — Price)**")
        p1, p2 = st.columns([2, 1])
        with p1:
            qty1 = st.number_input("500ML PACK — Quantity", min_value=0.0, step=1.0, key="qty_500")
            qty2 = st.number_input("1.5LTR PACK — Quantity", min_value=0.0, step=1.0, key="qty_15")
            qty3 = st.number_input("6LTR BOTTLE — Quantity", min_value=0.0, step=1.0, key="qty_6")
        with p2:
            price1 = st.number_input("500ML PACK — Price (per unit)", min_value=0.0, value=0.0, step=0.5, key="price_500")
            price2 = st.number_input("1.5LTR PACK — Price (per unit)", min_value=0.0, value=0.0, step=0.5, key="price_15")
            price3 = st.number_input("6LTR BOTTLE — Price (per unit)", min_value=0.0, value=0.0, step=0.5, key="price_6")

        submitted = st.form_submit_button("Generate Bill")

    if submitted:
        if not customer_name:
            st.warning("Customer name is required")
            return

        items = []
        if qty1 > 0 and price1 >= 0:
            items.append({"name": "500ML PACK", "qty": qty1, "price": price1})
        if qty2 > 0 and price2 >= 0:
            items.append({"name": "1.5LTR PACK", "qty": qty2, "price": price2})
        if qty3 > 0 and price3 >= 0:
            items.append({"name": "6LTR BOTTLE", "qty": qty3, "price": price3})

        if not items:
            st.warning("Add at least one item with quantity > 0")
            return

        bill_id, details, pdf = create_bill(customer_name, items, amount_paid)

        st.success(f"Bill #{bill_id} created for {customer_name}")
        metrics = st.columns(5)
        metrics[0].metric("Subtotal", f"{details['subtotal']:.2f}")
        metrics[1].metric("Advance Applied", f"{details['advance_applied']:.2f}")
        metrics[2].metric("Paid Now", f"{details['amount_paid']:.2f}")
        metrics[3].metric("Remaining After", f"{details['remaining_after']:.2f}")
        metrics[4].metric("Advance Balance", f"{details['advance_after']:.2f}")

        st.download_button("⬇️ Download Invoice PDF", data=pdf, file_name=f"invoice_{bill_id}.pdf", mime="application/pdf")

        # Auto-clear fields
        for k in ["cust_name", "amount_paid", "qty_500", "qty_15", "qty_6", "price_500", "price_15", "price_6"]:
            if k in st.session_state:
                del st.session_state[k]

        st.rerun()


    # Re-download by serial (moved here as requested)
    st.divider()
    st.subheader("🔁 Re-download Invoice by Serial")
    conn = get_conn()
    serial_to_dl = st.number_input("Bill Serial #", min_value=1, step=1, value=1)
    if st.button("Download PDF"):
        cur = conn.cursor()
        cur.execute("SELECT b.*, c.name as customer FROM bills b JOIN customers c ON c.id=b.customer_id WHERE b.id=?", (int(serial_to_dl),))
        b = cur.fetchone()
        if not b:
            st.error("Bill not found")
        else:
            items = pd.read_sql_query("SELECT item_name as item, quantity as qty, unit_price as price, line_total as total FROM bill_items WHERE bill_id=?", conn, params=(int(serial_to_dl),)).to_dict("records")
            pdf = build_invoice_pdf(serial=b["id"], customer_name=b["customer"], bill_date=datetime.fromisoformat(b["bill_date"]), items=items, subtotal=b["subtotal"], advance_applied=b["advance_applied"], amount_paid=b["amount_paid"], advance_after=b["advance_after"], remaining_after=b["remaining_after"])
            st.download_button("⬇️ Download Invoice PDF", pdf, file_name=f"invoice_{b['id']}.pdf", mime="application/pdf")
    conn.close()


def page_credit():
    st.title("🏦 Credit / Advance Ledger")
    conn = get_conn()
    df = pd.read_sql_query("SELECT c.name as Customer, l.advance as Advance, l.remaining as Remaining FROM customers c JOIN ledger l ON l.customer_id = c.id ORDER BY c.name", conn)
    conn.close()
    st.dataframe(df, use_container_width=True)


def page_history():
    st.title("📚 Billing History")
    conn = get_conn()
    df = pd.read_sql_query(
        "SELECT b.id as Serial, c.name as Customer, b.bill_date as Date, b.subtotal as Subtotal, b.amount_paid as Paid, b.remaining_after as Remaining, b.advance_after as Advance FROM bills b JOIN customers c ON c.id=b.customer_id ORDER BY b.id DESC",
        conn,
    )
    conn.close()
    st.dataframe(df, use_container_width=True)


def page_admin():
    st.title("⚙️ Admin")
    st.warning("Delete All Business Data will wipe customers, ledger, bills, and items. Users remain intact.")
    if st.button("🗑️ Delete All Business Data"):
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM bill_items")
        cur.execute("DELETE FROM bills")
        cur.execute("DELETE FROM ledger")
        cur.execute("DELETE FROM customers")
        conn.commit()
        conn.close()
        st.success("All business data deleted.")


# -----------------------------
# App entry
# -----------------------------

def main():
    st.set_page_config(page_title=f"{COMPANY_NAME} - Water POS", page_icon="💧", layout="wide")
    inject_css()
    init_db()

    user = st.session_state.get("user")
    if not user:
        page_auth()
        return

    # Top bar
    left, mid, right = st.columns([6, 2, 2])
    with left:
        st.markdown(f"### 💧 <span class='accent'>{COMPANY_NAME}</span>", unsafe_allow_html=True)
    with right:
        if st.button("Logout"):
            st.session_state.pop("user", None)
            st.rerun()


    # Sidebar navigation
    page = st.sidebar.selectbox("Navigate", ["Billing", "Credit", "History", "Admin"], index=0)

    # Show page content inside a card-styled container
    st.markdown("<div class='card'>", unsafe_allow_html=True)
    if page == "Billing":
        billing_form()
    elif page == "Credit":
        page_credit()
    elif page == "History":
        page_history()
    else:
        page_admin()
    st.markdown("</div>", unsafe_allow_html=True)


if __name__ == "__main__":
    main()
