# -*- coding: utf-8 -*-
"""
نظام الحسابات — الجواد لقطع غيار النقل الثقيل
نسخة Streamlit — وضعان:
  1) محلي: قاعدة بيانات SQLite (افتراضي — للشبكة الداخلية)
  2) سحابي: Supabase (للنشر على Streamlit Community Cloud)
     يُفعّل تلقائياً عند وجود الإعدادات في secrets:
       [supabase]
       url = "https://xxxx.supabase.co"
       key = "SERVICE_ROLE_KEY"
تشغيل:  streamlit run app.py
"""
import streamlit as st
import sqlite3
import hashlib
import json
import os
import re
from datetime import date

import pandas as pd

CURRENCY = 'ج.م'
BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, 'accounting.db')
LOGO_PATH = os.path.join(BASE, 'images', 'jawad.jpeg')

st.set_page_config(page_title='نظام الحسابات — الجواد', page_icon='💼', layout='wide')

CSS = """
<style>
.stApp, [data-testid="stSidebar"] { direction: rtl; text-align: right; }
h1, h2, h3 { text-align: right; }
.card { background:#fff; border:1px solid #e2e8f0; border-radius:12px; padding:16px 18px; text-align:center; height:100%; }
.card .lbl { color:#64748b; font-size:13px; margin-bottom:6px; }
.card .val { font-size:20px; font-weight:800; direction:ltr; }
div[data-testid="stDataFrame"] { direction: ltr; }
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)

# ================== تعريف الجداول ==================
TABLE_COLS = {
    'customers': ['name', 'phone', 'address', 'notes'],
    'suppliers': ['name', 'phone', 'address', 'notes'],
    'products':  ['name', 'unit', 'purchase_price', 'sale_price', 'stock', 'min_stock'],
    'invoices':  ['number', 'type', 'party_id', 'party_name', 'date', 'items',
                  'subtotal', 'discount', 'total', 'paid', 'notes'],
    'expenses':  ['date', 'category', 'amount', 'notes'],
    'users':     ['username', 'password_hash', 'name'],
}

TABLES = list(TABLE_COLS.keys())


def get_supa_cfg():
    """يقرأ إعدادات Supabase من secrets إن وُجدت"""
    try:
        s = st.secrets
        if 'supabase' in s:
            d = dict(s['supabase'])
            return d.get('url'), d.get('key')
        if 'SUPABASE_URL' in s:
            return s['SUPABASE_URL'], s['SUPABASE_KEY']
    except Exception:
        pass
    return None, None


# ================== الوضع المحلي: SQLite ==================
class SqliteBackend:
    name = 'sqlite'

    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def _init(self):
        with self.conn:
            self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS users(username TEXT PRIMARY KEY, password_hash TEXT, name TEXT);
            CREATE TABLE IF NOT EXISTS customers(
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT,
                phone TEXT DEFAULT '', address TEXT DEFAULT '', notes TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS suppliers(
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT,
                phone TEXT DEFAULT '', address TEXT DEFAULT '', notes TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS products(
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, unit TEXT DEFAULT '',
                purchase_price REAL DEFAULT 0, sale_price REAL DEFAULT 0,
                stock REAL DEFAULT 0, min_stock REAL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS invoices(
                id INTEGER PRIMARY KEY AUTOINCREMENT, number TEXT, type TEXT,
                party_id INTEGER, party_name TEXT, date TEXT, items TEXT DEFAULT '[]',
                subtotal REAL DEFAULT 0, discount REAL DEFAULT 0, total REAL DEFAULT 0,
                paid REAL DEFAULT 0, notes TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS expenses(
                id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, category TEXT,
                amount REAL DEFAULT 0, notes TEXT DEFAULT '');
            ''')

    def _prepare(self, table, values):
        allowed = list(TABLE_COLS[table]) + (['id'] if table != 'users' else [])
        cols = [c for c in allowed if c in values]
        vals = []
        for c in cols:
            v = values[c]
            if isinstance(v, (dict, list)):
                v = json.dumps(v, ensure_ascii=False)
            vals.append(v)
        return cols, vals

    def list(self, table, filters=None):
        rows = [dict(r) for r in self.conn.execute(f'SELECT * FROM {table}')]
        if filters:
            for k, v in filters.items():
                rows = [r for r in rows if str(r.get(k)) == str(v)]
        return rows

    def get(self, table, rid):
        r = self.conn.execute(f'SELECT * FROM {table} WHERE id=?', (rid,)).fetchone()
        return dict(r) if r else None

    def insert(self, table, values):
        cols, vals = self._prepare(table, values)
        with self.conn:
            cur = self.conn.execute(
                f'INSERT INTO {table} ({",".join(cols)}) VALUES({",".join("?" * len(cols))})', vals)
            rid = cur.lastrowid
        if table == 'users':  # جدول المستخدمين مفتاحه username وليس id
            return dict(zip(cols, vals))
        return self.get(table, rid)

    def update(self, table, rid, values):
        cols, vals = self._prepare(table, values)
        with self.conn:
            self.conn.execute(
                f'UPDATE {table} SET {",".join(c + "=?" for c in cols)} WHERE id=?',
                vals + [rid])
        return self.get(table, rid)

    def delete(self, table, rid):
        with self.conn:
            self.conn.execute(f'DELETE FROM {table} WHERE id=?', (rid,))

    def truncate(self, table):
        with self.conn:
            self.conn.execute(f'DELETE FROM {table}')

    def adjust_stock(self, pid, delta):
        with self.conn:
            self.conn.execute(
                'UPDATE products SET stock = COALESCE(stock,0) + ? WHERE id=?', (delta, pid))

    def get_user(self, username, pw_hash):
        r = self.conn.execute(
            'SELECT * FROM users WHERE username=? AND password_hash=?',
            (username, pw_hash)).fetchone()
        return dict(r) if r else None


# ================== الوضع السحابي: Supabase ==================
class SupabaseBackend:
    name = 'supabase'

    def __init__(self, url, key):
        from supabase import create_client
        self.c = create_client(url, key)

    def list(self, table, filters=None):
        q = self.c.table(table).select('*')
        if filters:
            for k, v in filters.items():
                q = q.eq(k, v)
        res = q.execute()
        return res.data or []

    def get(self, table, rid):
        res = self.c.table(table).select('*').eq('id', rid).limit(1).execute()
        return (res.data or [None])[0]

    def insert(self, table, values):
        res = self.c.table(table).insert(values).execute()
        return (res.data or [None])[0]

    def update(self, table, rid, values):
        res = self.c.table(table).update(values).eq('id', rid).execute()
        return (res.data or [None])[0]

    def delete(self, table, rid):
        self.c.table(table).delete().eq('id', rid).execute()

    def truncate(self, table):
        self.c.table(table).delete().neq('id', 0).execute()

    def adjust_stock(self, pid, delta):
        try:
            res = self.c.table('products').select('stock').eq('id', pid).limit(1).execute()
            stock = float((res.data or [{'stock': 0}])[0].get('stock') or 0)
        except Exception:
            stock = 0
        self.c.table('products').update({'stock': stock + delta}).eq('id', pid).execute()

    def get_user(self, username, pw_hash):
        res = self.c.table('users').select('*') \
            .eq('username', username).eq('password_hash', pw_hash).limit(1).execute()
        return (res.data or [None])[0]


_supa_url, _supa_key = get_supa_cfg()
if _supa_url and _supa_key:
    @st.cache_resource
    def _get_supa():
        return SupabaseBackend(_supa_url, _supa_key)
    backend = _get_supa()
else:
    backend = SqliteBackend()
    backend._init()

DB_MODE = backend.name


def ensure_admin_user():
    if not backend.list('users'):
        backend.insert('users', {'username': 'admin',
                                 'password_hash': hash_pw('admin123'),
                                 'name': 'المدير'})


def hash_pw(p):
    return hashlib.sha256(str(p).encode()).hexdigest()


def rows_to_dicts(rows):
    return list(rows)


def fmt(n):
    return f'{(n or 0):,.2f} {CURRENCY}'


def today():
    return date.today().isoformat()


def items_of(inv):
    it = inv.get('items') or []
    if isinstance(it, str):
        try:
            it = json.loads(it)
        except Exception:
            it = []
    return it


def next_invoice_number(inv_type):
    prefix = 'PUR' if inv_type == 'purchase' else 'INV'
    mx = 0
    for r in backend.list('invoices'):
        m = re.match(r'^[A-Z]+-(\d+)$', str(r.get('number') or ''))
        if m:
            mx = max(mx, int(m.group(1)))
    return f'{prefix}-{mx + 1:04d}'


def load_invoice(iid):
    return backend.get('invoices', iid)


def apply_items_effect(items, inv_type, direction):
    """direction=1 تطبيق (بيع يخصم/شراء يضيف) — direction=-1 تراجع"""
    for it in items or []:
        pid = it.get('productId')
        if not pid:
            continue
        qty = float(it.get('qty') or 0)
        delta = direction * (-qty if inv_type == 'sale' else qty)
        backend.adjust_stock(int(pid), delta)


def invoice_status_text(total, paid):
    total, paid = float(total or 0), float(paid or 0)
    if total > 0 and paid >= total:
        return 'مدفوعة'
    if paid > 0:
        return 'مدفوعة جزئياً'
    return 'غير مدفوعة'


def logo_b64():
    if os.path.exists(LOGO_PATH):
        import base64
        with open(LOGO_PATH, 'rb') as f:
            return base64.b64encode(f.read()).decode()
    return ''


def build_invoice_html(inv, items):
    logo = logo_b64()
    t = 'بيع' if inv['type'] == 'sale' else 'شراء'
    party_lbl = 'العميل' if inv['type'] == 'sale' else 'المورد'
    rows = ''.join(
        f"<tr><td>{i + 1}</td><td>{it.get('desc', '')}</td>"
        f"<td>{it.get('qty', 0)}</td><td>{fmt(it.get('price', 0))}</td>"
        f"<td>{fmt((it.get('qty') or 0) * (it.get('price') or 0))}</td></tr>"
        for i, it in enumerate(items))
    remain = (inv['total'] or 0) - (inv['paid'] or 0)
    return f"""<!DOCTYPE html><html lang="ar" dir="rtl"><head><meta charset="utf-8">
<title>{inv['number']}</title>
<style>body{{font-family:Arial,Tahoma; padding:26px; color:#111}}
.head{{text-align:center; margin-bottom:8px}}
img{{max-width:170px}}
table{{width:100%; border-collapse:collapse; margin-top:14px}}
th,td{{border:1px solid #bbb; padding:8px; text-align:right}}
th{{background:#f1f5f9}}
.totals{{margin-top:14px; font-size:15px; line-height:2}}</style></head><body>
{f'<div class="head"><img src="data:image/jpeg;base64,{logo}"/></div>' if logo else ''}
<div class="head"><h2>🧾 فاتورة {t} — {inv['number']}</h2>
<p>{party_lbl}: <b>{inv['party_name']}</b> — التاريخ: {inv['date']}</p></div>
<table><tr><th>#</th><th>الصنف</th><th>الكمية</th><th>السعر</th><th>الإجمالي</th></tr>{rows}</table>
<div class="totals">
إجمالي البنود: {fmt(inv['subtotal'])} — الخصم: {fmt(inv['discount'])} —
<b>الإجمالي النهائي: {fmt(inv['total'])}</b><br>
المدفوع: {fmt(inv['paid'])} — <b>المتبقي: {fmt(remain)}</b> —
الحالة: {invoice_status_text(inv['total'], inv['paid'])}
{f"<br>ملاحظات: {inv['notes']}" if inv.get('notes') else ''}
</div></body></html>"""


def build_statement_html(party, ptype, rows, totals):
    logo = logo_b64()
    inv_type_txt = 'فاتورة بيع' if ptype == 'customers' else 'فاتورة شراء'
    trs = ''.join(
        f"<tr><td>{r['n']}</td><td>{r['التاريخ']}</td><td><b>{r['رقم الفاتورة']}</b></td>"
        f"<td>{r['البيان']}</td><td>{r['مدين (عليه)']}</td><td>{r['دائن (مسدد)']}</td>"
        f"<td><b>{r['الرصيد']}</b></td></tr>" for r in rows)
    return f"""<!DOCTYPE html><html lang="ar" dir="rtl"><head><meta charset="utf-8">
<title>كشف حساب {party['name']}</title>
<style>body{{font-family:Arial,Tahoma; padding:26px; color:#111}}
.head{{text-align:center; margin-bottom:12px}}
img{{max-width:170px}}
table{{width:100%; border-collapse:collapse; margin-top:14px}}
th,td{{border:1px solid #bbb; padding:8px; text-align:right}}
th{{background:#f1f5f9}}
.tot{{font-weight:bold; background:#f8fafc}}</style></head><body>
{f'<div class="head"><img src="data:image/jpeg;base64,{logo}"/></div>' if logo else ''}
<div class="head"><h2>📄 كشف حساب {'عميل' if ptype == 'customers' else 'مورد'}</h2>
<p><b>{party['name']}</b> — تاريخ الإصدار: {today()}{(' — هاتف: ' + party['phone']) if party.get('phone') else ''}</p></div>
<table><tr><th>#</th><th>التاريخ</th><th>رقم الفاتورة</th><th>البيان</th>
<th>مدين (عليه)</th><th>دائن (مسدد)</th><th>الرصيد</th></tr>{trs}
<tr class="tot"><td colspan="4">الإجمالي</td><td>{fmt(totals['debit'])}</td>
<td>{fmt(totals['credit'])}</td><td>{fmt(totals['balance'])}</td></tr></table>
</body></html>"""


def cards_row(cards):
    cols = st.columns(len(cards))
    for col, c in zip(cols, cards):
        with col:
            st.markdown(
                f'<div class="card"><div class="lbl">{c["lbl"]}</div>'
                f'<div class="val" style="color:{c["clr"]}">{c["val"]}</div></div>',
                unsafe_allow_html=True)


def invoices_table():
    rows = sorted(backend.list('invoices'), key=lambda r: r.get('id') or 0, reverse=True)
    data = []
    for r in rows:
        total, paid = float(r['total'] or 0), float(r['paid'] or 0)
        data.append({
            'رقم الفاتورة': r['number'],
            'النوع': 'بيع' if r['type'] == 'sale' else 'شراء',
            'الطرف': r['party_name'] or '',
            'التاريخ': r['date'] or '',
            'الإجمالي': fmt(total),
            'الخصم': fmt(r['discount']) if r['discount'] else '—',
            'المدفوع': fmt(paid),
            'المتبقي': fmt(total - paid),
            'الحالة': invoice_status_text(total, paid),
        })
    return pd.DataFrame(data)


# ================== تسجيل الدخول ==================
def login_screen():
    col = st.columns([1, 2, 1])[1]
    with col:
        if os.path.exists(LOGO_PATH):
            st.image(LOGO_PATH, width=220)
        st.title('🔐 تسجيل الدخول')
        st.caption('نظام الحسابات — الجواد لقطع غيار النقل الثقيل')
        with st.form('login'):
            u = st.text_input('اسم المستخدم')
            p = st.text_input('كلمة المرور', type='password')
            ok = st.form_submit_button('دخول', use_container_width=True, type='primary')
        if ok:
            user = backend.get_user(u.strip(), hash_pw(p))
            if user:
                st.session_state.user = user
                st.rerun()
            else:
                st.error('❌ بيانات الدخول غير صحيحة')


# ================== لوحة التحكم ==================
PAGES = ['📊 لوحة التحكم', '🧾 الفواتير', '📄 كشف الحساب', '📦 المنتجات',
         '👥 العملاء', '🚚 الموردون', '💸 المصاريف']


def go(p):
    def cb():
        st.session_state.nav = p
    return cb


def dashboard():
    st.title('📊 لوحة التحكم')
    if os.path.exists(LOGO_PATH):
        st.image(LOGO_PATH, width=280)

    st.subheader('⚡ اختصارات سريعة')
    c1, c2, c3, c4 = st.columns(4)
    c1.button('🧾 فاتورة جديدة', use_container_width=True, on_click=go('🧾 الفواتير'))
    c2.button('👥 إدارة العملاء', use_container_width=True, on_click=go('👥 العملاء'))
    c3.button('🚚 إدارة الموردين', use_container_width=True, on_click=go('🚚 الموردون'))
    c4.button('💸 تسجيل مصروف', use_container_width=True, on_click=go('💸 المصاريف'))

    st.subheader('🧾 أحدث الفواتير')
    df = invoices_table()
    if df.empty:
        st.info('لا توجد فواتير بعد')
    else:
        st.dataframe(df.head(6), use_container_width=True, hide_index=True)

    with st.expander('💾 النسخ الاحتياطي'):
        data = {t: backend.list(t) for t in TABLES}
        st.download_button('⬇️ تصدير كل البيانات (JSON)',
                           json.dumps(data, ensure_ascii=False, indent=2, default=str),
                           file_name=f'backup-{today()}.json',
                           mime='application/json',
                           use_container_width=True)
        up = st.file_uploader('⬆️ استيراد نسخة احتياطية (سيتم استبدال كل البيانات)', type='json')
        if up is not None and st.checkbox('أنا متأكد — فهم أن الاستيراد سيستبدل كل البيانات'):
            if st.button('تنفيذ الاستيراد', type='primary'):
                try:
                    bak = json.loads(up.getvalue())
                    for t in TABLES:
                        backend.truncate(t)
                        for row in bak.get(t, []):
                            row = {k: v for k, v in row.items() if k in TABLE_COLS[t] or k == 'id'}
                            if t == 'users' and 'id' in row:
                                row.pop('id')
                            backend.insert(t, row)
                    st.toast('✅ تم استرجاع البيانات', icon='✅')
                    st.rerun()
                except Exception as e:
                    st.error(f'ملف غير صالح: {e}')


# ================== محرر الفواتير ==================
def invoice_editor(inv=None, kp='new'):
    """inv = بيانات فاتورة موجودة للتعديل، أو None لفاتورة جديدة"""
    t = st.radio('نوع الفاتورة', ['sale', 'purchase'],
                 format_func=lambda x: 'فاتورة بيع (عميل)' if x == 'sale' else 'فاتورة شراء (مورد)',
                 index=0 if (not inv or inv['type'] == 'sale') else 1,
                 key=kp + 'type', horizontal=True)
    table = 'customers' if t == 'sale' else 'suppliers'
    parties = sorted(backend.list(table), key=lambda p: p.get('name') or '')
    if not parties:
        st.warning(f"أضف {'عميلاً' if t == 'sale' else 'مورداً'} أولاً من صفحته")
        return

    pnames = [p['name'] for p in parties]
    default_idx = pnames.index(inv['party_name']) if (inv and inv['party_name'] in pnames) else 0
    party_name = st.selectbox('العميل' if t == 'sale' else 'المورد', pnames,
                              index=default_idx, key=kp + 'party')
    party = next(p for p in parties if p['name'] == party_name)

    c1, c2, c3 = st.columns(3)
    d_val = date.fromisoformat(inv['date']) if (inv and inv['date']) else date.today()
    c1.date_input('التاريخ', value=d_val, key=kp + 'date')
    c2.number_input('الخصم', min_value=0.0, step=0.01,
                    value=float(inv['discount'] or 0) if inv else 0.0, key=kp + 'disc')
    c3.number_input('المدفوع', min_value=0.0, step=0.01,
                    value=float(inv['paid'] or 0) if inv else 0.0, key=kp + 'paid')
    st.text_input('ملاحظات', value=inv['notes'] if inv else '', key=kp + 'notes')

    # ---- بنود الفاتورة ----
    products = sorted(backend.list('products'), key=lambda p: p.get('name') or '')
    pmap = {p['name']: p for p in products}
    id2name = {p['id']: p['name'] for p in products}
    prod_opts = [''] + list(pmap.keys())

    if inv:
        rows_df = [{'المنتج': id2name.get(it.get('productId'), ''),
                    'الصنف': it.get('desc', ''),
                    'الكمية': float(it.get('qty') or 0),
                    'السعر': float(it.get('price') or 0)} for it in items_of(inv)]
    else:
        rows_df = [{'المنتج': '', 'الصنف': '', 'الكمية': 1.0, 'السعر': 0.0}]

    st.markdown('**بنود الفاتورة** — اختر المنتج لتعبئة الاسم والسعر تلقائياً')
    items_df = st.data_editor(
        pd.DataFrame(rows_df),
        num_rows='dynamic', hide_index=True, use_container_width=True,
        column_config={
            'المنتج': st.column_config.SelectboxColumn(options=prod_opts, width='medium'),
            'الصنف': st.column_config.TextColumn(width='medium'),
            'الكمية': st.column_config.NumberColumn(min_value=0, step=1),
            'السعر': st.column_config.NumberColumn(min_value=0.0, step=0.01, format='%.2f'),
        }, key=kp + 'items')

    # تعبئة تلقائية من المنتج المختار
    filled = items_df.copy()
    for i, r in filled.iterrows():
        pn = r['المنتج']
        if pn and pn in pmap:
            p = pmap[pn]
            if not str(r['الصنف'] or '').strip():
                filled.at[i, 'الصنف'] = p['name']
            if not r['السعر']:
                filled.at[i, 'السعر'] = float(p['sale_price'] if t == 'sale' else p['purchase_price'])
    filled = filled.fillna({'المنتج': '', 'الصنف': '', 'الكمية': 0, 'السعر': 0})
    valid = filled[(filled['الصنف'].astype(str).str.strip() != '') & (filled['الكمية'] > 0)]

    subtotal = float((valid['الكمية'] * valid['السعر']).sum())
    discount = float(st.session_state.get(kp + 'disc') or 0)
    paid = float(st.session_state.get(kp + 'paid') or 0)
    total = max(0.0, subtotal - discount)
    cards_row([
        {'lbl': 'إجمالي البنود', 'val': fmt(subtotal), 'clr': '#0f172a'},
        {'lbl': 'الخصم', 'val': fmt(discount), 'clr': '#dc2626'},
        {'lbl': 'الإجمالي النهائي', 'val': fmt(total), 'clr': '#2563eb'},
        {'lbl': 'المتبقي', 'val': fmt(total - paid), 'clr': '#d97706'},
    ])

    # ---- الحفظ ----
    shortage, can_save = [], True
    if st.button('💾 حفظ الفاتورة', key=kp + 'save', type='primary'):
        if valid.empty:
            st.error('أضف بنداً واحداً على الأقل (صنف + كمية)')
        else:
            items_data = [{
                'desc': str(r['الصنف']).strip(),
                'qty': float(r['الكمية']),
                'price': float(r['السعر']),
                'productId': pmap[r['المنتج']]['id'] if r['المنتج'] in pmap else '',
            } for _, r in valid.iterrows()]

            if t == 'sale':
                need = {}
                for it in items_data:
                    if it['productId']:
                        need[it['productId']] = need.get(it['productId'], 0) + it['qty']
                for pid, q in need.items():
                    p = next((x for x in products if x['id'] == pid), None)
                    if p and float(p['stock'] or 0) < q:
                        shortage.append(f"{p['name']} (المتاح: {p['stock']})")
                if shortage and not st.session_state.get(kp + 'force'):
                    st.warning('الكمية المطلوبة أكبر من المخزون: ' + ' | '.join(shortage))
                    st.checkbox('متابعة رغم نقص المخزون', key=kp + 'force')
                    can_save = False

            if can_save:
                if inv:  # تراجع تأثير الفاتورة القديمة على المخزون
                    apply_items_effect(items_of(inv), inv['type'], -1)
                apply_items_effect(items_data, t, +1)
                number = inv['number'] if inv else next_invoice_number(t)
                d = st.session_state[kp + 'date'].isoformat()
                vals = {'number': number, 'type': t, 'party_id': party['id'],
                        'party_name': party['name'], 'date': d,
                        'items': items_data, 'subtotal': subtotal,
                        'discount': discount, 'total': total, 'paid': paid,
                        'notes': st.session_state.get(kp + 'notes') or ''}
                if inv:
                    backend.update('invoices', inv['id'], vals)
                else:
                    backend.insert('invoices', vals)
                for k in [k for k in list(st.session_state) if k.startswith(kp)]:
                    del st.session_state[k]
                st.toast('✅ تم حفظ الفاتورة ' + number, icon='✅')
                st.rerun()


def pick_invoice(label):
    rows = sorted(backend.list('invoices'), key=lambda r: r.get('id') or 0, reverse=True)
    if not rows:
        return None
    opts = {f"{r['number']} — {r['party_name']} — {r['date']}": r['id'] for r in rows}
    pick = st.selectbox(label, ['— اختر —'] + list(opts.keys()))
    if pick == '— اختر —':
        return None
    return load_invoice(opts[pick])


def invoices_page():
    st.title('🧾 الفواتير')

    c1, c2 = st.columns([1, 3])
    tf = c1.selectbox('النوع', ['الكل', 'بيع', 'شراء'])
    q = c2.text_input('🔍 بحث (رقم الفاتورة / الطرف)', '').strip()

    df = invoices_table()
    if tf != 'الكل':
        df = df[df['النوع'] == tf]
    if q:
        m = df.apply(lambda r: q in str(r['رقم الفاتورة']) or q in str(r['الطرف']), axis=1)
        df = df[m]
    st.dataframe(df, use_container_width=True, hide_index=True)

    with st.expander('➕ فاتورة جديدة', expanded=False):
        invoice_editor(None, kp='new')

    tab_v, tab_e, tab_p, tab_d = st.tabs(['👁️ التفاصيل', '✏️ تعديل', '💵 تسديد', '🗑️ حذف'])

    with tab_v:
        inv = pick_invoice('اختر الفاتورة')
        if inv:
            items = items_of(inv)
            it_df = pd.DataFrame([{
                '#': i + 1, 'الصنف': it.get('desc', ''),
                'الكمية': it.get('qty', 0), 'السعر': fmt(it.get('price', 0)),
                'الإجمالي': fmt((it.get('qty') or 0) * (it.get('price') or 0)),
            } for i, it in enumerate(items)])
            st.dataframe(it_df, use_container_width=True, hide_index=True)
            remain = (inv['total'] or 0) - (inv['paid'] or 0)
            cards_row([
                {'lbl': 'إجمالي البنود', 'val': fmt(inv['subtotal']), 'clr': '#0f172a'},
                {'lbl': 'الخصم', 'val': fmt(inv['discount']), 'clr': '#dc2626'},
                {'lbl': 'الإجمالي النهائي', 'val': fmt(inv['total']), 'clr': '#2563eb'},
                {'lbl': 'المدفوع', 'val': fmt(inv['paid']), 'clr': '#16a34a'},
                {'lbl': 'المتبقي', 'val': fmt(remain), 'clr': '#dc2626' if remain > 0 else '#16a34a'},
                {'lbl': 'الحالة', 'val': invoice_status_text(inv['total'], inv['paid']), 'clr': '#0f172a'},
            ])
            if inv['notes']:
                st.info('📝 ملاحظات: ' + inv['notes'])
            st.download_button('⬇️ تحميل الفاتورة (HTML للطباعة والمشاركة)',
                               build_invoice_html(inv, items),
                               file_name=f"{inv['number']}.html", mime='text/html')

    with tab_e:
        inv = pick_invoice('اختر الفاتورة للتعديل')
        if inv:
            invoice_editor(inv, kp=f"e{inv['id']}")

    with tab_p:
        inv = pick_invoice('اختر الفاتورة للتسديد')
        if inv:
            remain = (inv['total'] or 0) - (inv['paid'] or 0)
            st.info(f"الإجمالي: {fmt(inv['total'])} — المدفوع: {fmt(inv['paid'])} — المتبقي: {fmt(remain)}")
            amt = st.number_input('المبلغ المسدد الآن', min_value=0.0, step=0.01,
                                  value=float(max(remain, 0)), key=f'pay{inv["id"]}')
            if st.button('💾 تسجيل الدفع', key=f'paybtn{inv["id"]}', type='primary'):
                backend.update('invoices', inv['id'],
                               {'paid': float(inv['paid'] or 0) + amt})
                st.toast('✅ تم تسجيل الدفع', icon='✅')
                st.rerun()

    with tab_d:
        inv = pick_invoice('اختر الفاتورة للحذف')
        if inv:
            st.warning(f"سيتم حذف الفاتورة {inv['number']} وإرجاع كمياتها للمخزون.")
            if st.checkbox('أنا متأكد من الحذف', key=f'delchk{inv["id"]}'):
                if st.button('🗑️ حذف نهائي', key=f'delbtn{inv["id"]}', type='primary'):
                    apply_items_effect(items_of(inv), inv['type'], -1)
                    backend.delete('invoices', inv['id'])
                    st.toast('🗑️ تم حذف الفاتورة', icon='✅')
                    st.rerun()


# ================== كشف الحساب ==================
def statement_page():
    st.title('📄 كشف الحساب')
    ptype = st.radio('النوع', ['customers', 'suppliers'],
                     format_func=lambda x: 'كشف حساب عميل' if x == 'customers' else 'كشف حساب مورد',
                     horizontal=True)
    parties = sorted(backend.list(ptype), key=lambda p: p.get('name') or '')
    if not parties:
        st.warning('لا توجد أسماء بعد — أضفها من صفحتها أولاً')
        return
    names = [p['name'] for p in parties]
    name = st.selectbox('الاسم', names)
    party = next(p for p in parties if p['name'] == name)

    inv_type = 'sale' if ptype == 'customers' else 'purchase'
    invs = [r for r in backend.list('invoices')
            if r.get('type') == inv_type
            and str(r.get('party_id')) == str(party['id'])]
    invs.sort(key=lambda r: (r.get('date') or '', r.get('id') or 0))
    inv_txt = 'فاتورة بيع' if ptype == 'customers' else 'فاتورة شراء'

    run_bal, t_debit, t_credit = 0.0, 0.0, 0.0
    rows = []
    for i, r in enumerate(invs):
        debit, credit = float(r['total'] or 0), float(r['paid'] or 0)
        run_bal += debit - credit
        t_debit += debit
        t_credit += credit
        rows.append({
            'n': i + 1, 'التاريخ': r['date'] or '', 'رقم الفاتورة': r['number'],
            'البيان': inv_txt + (f" — {r['notes']}" if r['notes'] else ''),
            'مدين (عليه)': fmt(debit), 'دائن (مسدد)': fmt(credit),
            'الرصيد': fmt(run_bal),
        })

    cards_row([
        {'lbl': 'إجمالي الفواتير (مدين)', 'val': fmt(t_debit), 'clr': '#d97706'},
        {'lbl': 'إجمالي المسدد (دائن)', 'val': fmt(t_credit), 'clr': '#16a34a'},
        {'lbl': 'الرصيد المستحق', 'val': fmt(run_bal), 'clr': '#dc2626' if run_bal > 0 else '#16a34a'},
    ])

    if not rows:
        st.info('لا توجد فواتير لهذا الحساب')
        return

    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)

    totals = {'debit': t_debit, 'credit': t_credit, 'balance': run_bal}
    csv_bytes = df.to_csv(index=False).encode('utf-8-sig')
    c1, c2 = st.columns(2)
    c1.download_button('⬇️ تحميل CSV (Excel)', csv_bytes,
                       file_name=f"statement-{party['name']}.csv", mime='text/csv')
    c2.download_button('⬇️ تحميل نسخة للطباعة/المشاركة (HTML)',
                       build_statement_html(party, ptype, rows, totals),
                       file_name=f"statement-{party['name']}.html", mime='text/html')


# ================== العملاء / الموردون ==================
def party_page(table, title, emoji, label):
    st.title(f'{emoji} {title}')
    q = st.text_input('🔍 بحث بالاسم أو الهاتف', '').strip()
    rows = sorted(backend.list(table), key=lambda r: r.get('name') or '')
    if q:
        rows = [r for r in rows if q in (r.get('name') or '') or q in (r.get('phone') or '')]
    df = pd.DataFrame([{'الاسم': r['name'], 'الهاتف': r.get('phone') or '—',
                        'العنوان': r.get('address') or '—', 'ملاحظات': r.get('notes') or '—'}
                       for r in rows])
    if df.empty:
        st.info('لا توجد بيانات بعد')
    else:
        st.dataframe(df, use_container_width=True, hide_index=True)

    a, e, d = st.tabs(['➕ إضافة', '✏️ تعديل', '🗑️ حذف'])
    with a:
        with st.form(f'add_{table}'):
            n = st.text_input(f'اسم {label}', key=f'{table}_an')
            ph = st.text_input('رقم الهاتف', key=f'{table}_ap')
            ad = st.text_input('العنوان', key=f'{table}_aa')
            nt = st.text_area('ملاحظات', key=f'{table}_ant')
            if st.form_submit_button('💾 حفظ', type='primary'):
                if not n.strip():
                    st.error('الاسم مطلوب')
                else:
                    backend.insert(table, {'name': n.strip(), 'phone': ph.strip(),
                                           'address': ad.strip(), 'notes': nt.strip()})
                    st.toast('✅ تمت الإضافة', icon='✅')
                    st.rerun()
    with e:
        all_rows = sorted(backend.list(table), key=lambda r: r.get('name') or '')
        if not all_rows:
            st.info('لا يوجد ما يتم تعديله')
        else:
            names = [r['name'] for r in all_rows]
            sel = st.selectbox(f'اختر {label} للتعديل', names, key=f'{table}_esel')
            row = next(r for r in all_rows if r['name'] == sel)
            k = f'{table}_e{row["id"]}'
            n = st.text_input(f'اسم {label}', value=row['name'], key=k + 'n')
            ph = st.text_input('رقم الهاتف', value=row.get('phone') or '', key=k + 'p')
            ad = st.text_input('العنوان', value=row.get('address') or '', key=k + 'a')
            nt = st.text_area('ملاحظات', value=row.get('notes') or '', key=k + 't')
            if st.button('💾 حفظ التعديل', key=k + 'btn', type='primary'):
                if not n.strip():
                    st.error('الاسم مطلوب')
                else:
                    backend.update(table, row['id'],
                                   {'name': n.strip(), 'phone': ph.strip(),
                                    'address': ad.strip(), 'notes': nt.strip()})
                    st.toast('✅ تم التعديل', icon='✅')
                    st.rerun()
    with d:
        all_rows = sorted(backend.list(table), key=lambda r: r.get('name') or '')
        if not all_rows:
            st.info('لا يوجد ما يتم حذفه')
        else:
            sel = st.selectbox(f'اختر {label} للحذف', [r['name'] for r in all_rows],
                               key=f'{table}_dsel')
            row = next(r for r in all_rows if r['name'] == sel)
            st.warning(f"سيتم حذف: {row['name']} (فواتيره القديمة ستبقى محفوظة باسمه)")
            if st.checkbox('أنا متأكد من الحذف', key=f'{table}_dchk'):
                if st.button('🗑️ حذف نهائي', key=f'{table}_dbtn', type='primary'):
                    backend.delete(table, row['id'])
                    st.toast('🗑️ تم الحذف', icon='✅')
                    st.rerun()


# ================== المنتجات ==================
def products_page():
    st.title('📦 المنتجات والمخزون')
    st.caption('الكمية تتخصم تلقائياً مع فواتير البيع وتتزود مع فواتير الشراء')
    q = st.text_input('🔍 بحث باسم المنتج', '').strip()
    rows = sorted(backend.list('products'), key=lambda r: r.get('name') or '')
    if q:
        rows = [r for r in rows if q in (r.get('name') or '')]
    data = []
    for r in rows:
        s, m = float(r.get('stock') or 0), float(r.get('min_stock') or 0)
        data.append({
            'اسم المنتج': r['name'], 'الوحدة': r.get('unit') or '—',
            'سعر الشراء': fmt(r.get('purchase_price')), 'سعر البيع': fmt(r.get('sale_price')),
            'المخزون': f'⚠️ {s}' if (m > 0 and s <= m) else f'{s}',
            'حد التنبيه': m,
        })
    df = pd.DataFrame(data)
    if df.empty:
        st.info('لا توجد منتجات بعد')
    else:
        st.dataframe(df, use_container_width=True, hide_index=True)

    a, e, d = st.tabs(['➕ إضافة', '✏️ تعديل', '🗑️ حذف'])
    UNITS = ['قطعة', 'كرتونة', 'كجم', 'جرام', 'لتر', 'متر', 'أخرى']
    with a:
        with st.form('add_product'):
            n = st.text_input('اسم المنتج')
            u = st.selectbox('الوحدة', UNITS)
            pp = st.number_input('سعر الشراء', min_value=0.0, step=0.01)
            sp = st.number_input('سعر البيع', min_value=0.0, step=0.01)
            sk = st.number_input('الكمية بالمخزون', min_value=0.0, step=1.0)
            mn = st.number_input('حد التنبيه', min_value=0.0, step=1.0)
            if st.form_submit_button('💾 حفظ', type='primary'):
                if not n.strip():
                    st.error('الاسم مطلوب')
                else:
                    backend.insert('products', {'name': n.strip(), 'unit': u,
                                                'purchase_price': pp, 'sale_price': sp,
                                                'stock': sk, 'min_stock': mn})
                    st.toast('✅ تمت الإضافة', icon='✅')
                    st.rerun()
    with e:
        all_rows = sorted(backend.list('products'), key=lambda r: r.get('name') or '')
        if not all_rows:
            st.info('لا يوجد ما يتم تعديله')
        else:
            sel = st.selectbox('اختر المنتج للتعديل', [r['name'] for r in all_rows],
                               key='p_esel')
            row = next(r for r in all_rows if r['name'] == sel)
            k = f'pe{row["id"]}'
            n = st.text_input('اسم المنتج', value=row['name'], key=k + 'n')
            u = st.selectbox('الوحدة', UNITS,
                             index=UNITS.index(row['unit']) if row.get('unit') in UNITS else 6,
                             key=k + 'u')
            pp = st.number_input('سعر الشراء', min_value=0.0, step=0.01,
                                 value=float(row.get('purchase_price') or 0), key=k + 'pp')
            sp = st.number_input('سعر البيع', min_value=0.0, step=0.01,
                                 value=float(row.get('sale_price') or 0), key=k + 'sp')
            sk = st.number_input('الكمية بالمخزون', step=1.0,
                                 value=float(row.get('stock') or 0), key=k + 'sk')
            mn = st.number_input('حد التنبيه', step=1.0,
                                 value=float(row.get('min_stock') or 0), key=k + 'mn')
            if st.button('💾 حفظ التعديل', key=k + 'btn', type='primary'):
                if not n.strip():
                    st.error('الاسم مطلوب')
                else:
                    backend.update('products', row['id'],
                                   {'name': n.strip(), 'unit': u, 'purchase_price': pp,
                                    'sale_price': sp, 'stock': sk, 'min_stock': mn})
                    st.toast('✅ تم التعديل', icon='✅')
                    st.rerun()
    with d:
        all_rows = sorted(backend.list('products'), key=lambda r: r.get('name') or '')
        if not all_rows:
            st.info('لا يوجد ما يتم حذفه')
        else:
            sel = st.selectbox('اختر المنتج للحذف', [r['name'] for r in all_rows], key='p_dsel')
            row = next(r for r in all_rows if r['name'] == sel)
            st.warning(f"سيتم حذف: {row['name']} (كمياته في الفواتير القديمة ستبقى سليمة)")
            if st.checkbox('أنا متأكد من الحذف', key='p_dchk'):
                if st.button('🗑️ حذف نهائي', key='p_dbtn', type='primary'):
                    backend.delete('products', row['id'])
                    st.toast('🗑️ تم الحذف', icon='✅')
                    st.rerun()


# ================== المصاريف ==================
def expenses_page():
    st.title('💸 المصاريف اليومية')
    CATS = ['إيجار', 'كهرباء', 'مياه', 'رواتب', 'مواصلات', 'صيانة',
            'اتصالات وإنترنت', 'مشتريات متنوعة', 'أخرى']
    q = st.text_input('🔍 بحث بالتصنيف أو البيان', '').strip()
    rows = sorted(backend.list('expenses'),
                  key=lambda r: (r.get('date') or '', r.get('id') or 0), reverse=True)
    if q:
        rows = [r for r in rows if q in (r.get('category') or '') or q in (r.get('notes') or '')]
    df = pd.DataFrame([{'التاريخ': r.get('date'), 'التصنيف': r.get('category'),
                        'المبلغ': fmt(r.get('amount')), 'البيان': r.get('notes') or '—'}
                       for r in rows])
    if df.empty:
        st.info('لا توجد مصاريف بعد')
    else:
        st.dataframe(df, use_container_width=True, hide_index=True)

    a, e, d = st.tabs(['➕ إضافة', '✏️ تعديل', '🗑️ حذف'])
    with a:
        with st.form('add_exp'):
            d1, d2 = st.columns(2)
            dt = d1.date_input('التاريخ', value=date.today())
            cat = d2.selectbox('التصنيف', CATS)
            amt = st.number_input('المبلغ', min_value=0.0, step=0.01)
            nt = st.text_area('البيان / ملاحظات')
            if st.form_submit_button('💾 حفظ', type='primary'):
                backend.insert('expenses', {'date': dt.isoformat(), 'category': cat,
                                            'amount': amt, 'notes': nt.strip()})
                st.toast('✅ تمت الإضافة', icon='✅')
                st.rerun()
    with e:
        all_rows = sorted(backend.list('expenses'),
                          key=lambda r: (r.get('date') or '', r.get('id') or 0), reverse=True)
        if not all_rows:
            st.info('لا يوجد ما يتم تعديله')
        else:
            opts = {f"{r.get('date')} — {r.get('category')} — {fmt(r.get('amount'))}": r['id']
                    for r in all_rows}
            sel = st.selectbox('اختر المصروف للتعديل', list(opts.keys()), key='e_esel')
            row = next(r for r in all_rows if r['id'] == opts[sel])
            k = f'ee{row["id"]}'
            dt = st.date_input('التاريخ', value=date.fromisoformat(row['date']), key=k + 'd')
            cat = st.selectbox('التصنيف', CATS,
                               index=CATS.index(row['category']) if row.get('category') in CATS else 8,
                               key=k + 'c')
            amt = st.number_input('المبلغ', min_value=0.0, step=0.01,
                                  value=float(row.get('amount') or 0), key=k + 'a')
            nt = st.text_area('البيان / ملاحظات', value=row.get('notes') or '', key=k + 't')
            if st.button('💾 حفظ التعديل', key=k + 'btn', type='primary'):
                backend.update('expenses', row['id'],
                               {'date': dt.isoformat(), 'category': cat,
                                'amount': amt, 'notes': nt.strip()})
                st.toast('✅ تم التعديل', icon='✅')
                st.rerun()
    with d:
        all_rows = sorted(backend.list('expenses'),
                          key=lambda r: (r.get('date') or '', r.get('id') or 0), reverse=True)
        if not all_rows:
            st.info('لا يوجد ما يتم حذفه')
        else:
            opts = {f"{r.get('date')} — {r.get('category')} — {fmt(r.get('amount'))}": r['id']
                    for r in all_rows}
            sel = st.selectbox('اختر المصروف للحذف', list(opts.keys()), key='e_dsel')
            row = next(r for r in all_rows if r['id'] == opts[sel])
            st.warning(f"سيتم حذف: {row.get('category')} — {fmt(row.get('amount'))}")
            if st.checkbox('أنا متأكد من الحذف', key='e_dchk'):
                if st.button('🗑️ حذف نهائي', key='e_dbtn', type='primary'):
                    backend.delete('expenses', row['id'])
                    st.toast('🗑️ تم الحذف', icon='✅')
                    st.rerun()


# ================== التشغيل ==================
if 'user' not in st.session_state:
    ensure_admin_user()
    login_screen()
    st.stop()

with st.sidebar:
    st.markdown('### 💼 نظام الحسابات')
    if DB_MODE == 'supabase':
        st.caption('☁️ متصل بقاعدة البيانات السحابية')
    else:
        st.caption('💻 قاعدة بيانات محلية (SQLite)')
    if 'nav' not in st.session_state:
        st.session_state.nav = PAGES[0]
    page = st.radio('التنقل', PAGES, key='nav', label_visibility='collapsed')
    st.divider()
    st.write(f"👤 {st.session_state.user['name']}")
    if st.button('🚪 تسجيل الخروج', use_container_width=True):
        del st.session_state['user']
        st.rerun()

if page == PAGES[0]:
    dashboard()
elif page == PAGES[1]:
    invoices_page()
elif page == PAGES[2]:
    statement_page()
elif page == PAGES[3]:
    products_page()
elif page == PAGES[4]:
    party_page('customers', 'العملاء', '👥', 'عميل')
elif page == PAGES[5]:
    party_page('suppliers', 'الموردون', '🚚', 'مورد')
else:
    expenses_page()
