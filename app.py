import streamlit as st
import pandas as pd
from datetime import datetime, date, timedelta
import os
from supabase import create_client, Client
import plotly.express as px
import plotly.graph_objects as go
from fpdf import FPDF
import base64
import traceback

# -------------------- Supabase Initialization --------------------
@st.cache_resource
def init_supabase():
    url = st.secrets["SUPABASE_URL"]
    key = st.secrets["SUPABASE_KEY"]
    return create_client(url, key)

supabase = init_supabase()

# -------------------- Session State Initialization --------------------
def init_session():
    defaults = {
        'authenticated': False,
        'current_shift_id': None,
        'current_shift_name': None,
        'current_date': date.today(),
        'page': 'Dashboard',
        'expense_rows': [],
        'payment_rows': [],
        'purchase_rows': [],
        'return_rows': [],
        'withdrawal_rows': []
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val

init_session()

# -------------------- Authentication --------------------
def login():
    st.title("🔐 Medical Store Login")
    with st.form("login_form"):
        password = st.text_input("Password", type="password")
        if st.form_submit_button("Login"):
            if password == st.secrets.get("APP_PASSWORD", "admin123"):
                st.session_state.authenticated = True
                st.rerun()
            else:
                st.error("Incorrect password")

if not st.session_state.authenticated:
    login()
    st.stop()

# -------------------- Helper Functions --------------------
def safe_supabase_call(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except Exception as e:
        st.error(f"Database error: {str(e)}")
        st.stop()

def fetch_expense_heads():
    return pd.DataFrame(supabase.table("expense_heads").select("*").execute().data)

def fetch_vendors():
    return pd.DataFrame(supabase.table("vendors").select("*").execute().data)

def fetch_shifts(date_selected):
    response = supabase.table("shifts").select("*").eq("date", date_selected.isoformat()).execute()
    return pd.DataFrame(response.data)

def get_or_create_shift(date_selected, shift_name):
    # Check if shift exists
    response = supabase.table("shifts").select("*").eq("date", date_selected.isoformat()).eq("shift_name", shift_name).execute()
    if response.data:
        return response.data[0]['id']
    
    # Determine opening cash
    prev_shifts = supabase.table("shifts").select("*").eq("date", date_selected.isoformat()).lt("shift_name", shift_name).order("shift_name").execute()
    opening = 0
    if prev_shifts.data:
        last_shift = prev_shifts.data[-1]
        opening = last_shift.get('expected_cash') or last_shift.get('closing_cash_entered') or 0
    
    data = {
        "date": date_selected.isoformat(),
        "shift_name": shift_name,
        "opening_cash": opening,
        "total_sale": 0,
        "status": "open"
    }
    response = supabase.table("shifts").insert(data).execute()
    return response.data[0]['id']

def calculate_expected_cash(shift_id):
    shift = supabase.table("shifts").select("*").eq("id", shift_id).execute().data[0]
    opening = shift['opening_cash'] or 0
    sale = shift['total_sale'] or 0
    
    # Expenses from sales cash
    exp_sales = supabase.table("expenses").select("amount").eq("shift_id", shift_id).eq("source", "sales_cash").execute()
    total_exp_sales = sum([e['amount'] for e in exp_sales.data])
    
    # Vendor payments from sales cash
    pay_sales = supabase.table("vendor_payments").select("amount").eq("shift_id", shift_id).eq("source", "sales_cash").execute()
    total_pay_sales = sum([p['amount'] for p in pay_sales.data])
    
    # Purchases cash from sales cash
    pur_sales = supabase.table("purchases").select("amount").eq("shift_id", shift_id).eq("payment_type", "cash").eq("source_if_cash", "sales_cash").execute()
    total_pur_sales = sum([p['amount'] for p in pur_sales.data])
    
    # Withdrawals
    wd = supabase.table("withdrawals").select("amount").eq("shift_id", shift_id).execute()
    total_wd = sum([w['amount'] for w in wd.data])
    
    expected = opening + sale - (total_exp_sales + total_pay_sales + total_pur_sales + total_wd)
    return expected

def update_expected_cash(shift_id):
    expected = calculate_expected_cash(shift_id)
    supabase.table("shifts").update({"expected_cash": expected}).eq("id", shift_id).execute()

def close_shift(shift_id, closing_cash):
    shift = supabase.table("shifts").select("*").eq("id", shift_id).execute().data[0]
    expected = shift['expected_cash'] or calculate_expected_cash(shift_id)
    
    if closing_cash < expected:
        shortage_head = supabase.table("expense_heads").select("id").eq("name", "Cash Shortage").execute().data[0]['id']
        shortage_amt = expected - closing_cash
        supabase.table("expenses").insert({
            "shift_id": shift_id,
            "expense_head_id": shortage_head,
            "amount": shortage_amt,
            "source": "sales_cash",
            "description": "Auto-recorded cash shortage"
        }).execute()
        update_expected_cash(shift_id)
    
    supabase.table("shifts").update({
        "closing_cash_entered": closing_cash,
        "status": "closed",
        "closed_at": datetime.now().isoformat()
    }).eq("id", shift_id).execute()

def record_owner_ledger(amount, description, shift_id=None):
    """amount positive = owner puts in, negative = owner withdraws"""
    supabase.table("owner_ledger").insert({
        "transaction_date": datetime.now().isoformat(),
        "amount": amount,
        "description": description,
        "shift_id": shift_id
    }).execute()

# -------------------- Navigation --------------------
st.sidebar.title("Medical Store")
pages = ["Dashboard", "Heads Setup", "Shift Recording", "Reports"]
choice = st.sidebar.radio("Go to", pages, index=pages.index(st.session_state.page))
st.session_state.page = choice

# -------------------- Dashboard Page --------------------
if st.session_state.page == "Dashboard":
    st.title("📊 Dashboard")
    
    col1, col2 = st.columns([1, 3])
    with col1:
        selected_date = st.date_input("Select Date", value=date.today())
    with col2:
        st.subheader(f"Summary for {selected_date}")
    
    shifts = fetch_shifts(selected_date)
    if shifts.empty:
        st.info("No shifts recorded for this date.")
    else:
        # KPI cards
        total_sale = shifts['total_sale'].sum()
        shift_ids = shifts['id'].tolist()
        
        # Fetch totals
        expenses = supabase.table("expenses").select("amount").in_("shift_id", shift_ids).execute()
        total_expenses = sum([e['amount'] for e in expenses.data]) if expenses.data else 0
        
        withdrawals = supabase.table("withdrawals").select("amount").in_("shift_id", shift_ids).execute()
        total_withdrawals = sum([w['amount'] for w in withdrawals.data]) if withdrawals.data else 0
        
        payments = supabase.table("vendor_payments").select("amount").in_("shift_id", shift_ids).execute()
        total_payments = sum([p['amount'] for p in payments.data]) if payments.data else 0
        
        # Available cash (closing of last shift)
        last_shift = shifts.iloc[-1]
        available_cash = last_shift['expected_cash'] if pd.notna(last_shift['expected_cash']) else 0
        
        cola, colb, colc, cold, cole = st.columns(5)
        cola.metric("Total Sale", f"₹{total_sale:,.2f}")
        colb.metric("Expenses", f"₹{total_expenses:,.2f}")
        colc.metric("Withdrawals", f"₹{total_withdrawals:,.2f}")
        cold.metric("Vendor Payments", f"₹{total_payments:,.2f}")
        cole.metric("Available Cash", f"₹{available_cash:,.2f}")
        
        # Shift breakdown
        st.subheader("Shifts Breakdown")
        for _, shift in shifts.iterrows():
            with st.expander(f"{shift['shift_name']} Shift - {'Closed' if shift['status']=='closed' else 'Open'}"):
                st.write(f"Opening Cash: ₹{shift['opening_cash']:,.2f}")
                st.write(f"Total Sale: ₹{shift['total_sale']:,.2f}")
                st.write(f"Expected Cash: ₹{shift['expected_cash']:,.2f}" if pd.notna(shift['expected_cash']) else "Expected Cash: Not calculated")
                if pd.notna(shift['closing_cash_entered']):
                    st.write(f"Closing Cash Entered: ₹{shift['closing_cash_entered']:,.2f}")

# -------------------- Heads Setup Page --------------------
elif st.session_state.page == "Heads Setup":
    st.title("⚙️ Heads & Vendors")
    
    tab1, tab2 = st.tabs(["Expense Heads", "Vendors"])
    
    with tab1:
        st.subheader("Expense Heads")
        with st.form("add_head"):
            name = st.text_input("Head Name")
            desc = st.text_area("Description")
            if st.form_submit_button("Add Head"):
                if name:
                    supabase.table("expense_heads").insert({"name": name, "description": desc}).execute()
                    st.success("Added!")
                    st.rerun()
        heads = fetch_expense_heads()
        st.dataframe(heads[['name', 'description']], use_container_width=True)
    
    with tab2:
        st.subheader("Vendors")
        with st.form("add_vendor"):
            name = st.text_input("Vendor Name")
            contact = st.text_input("Contact")
            opening = st.number_input("Opening Balance", value=0.0)
            if st.form_submit_button("Add Vendor"):
                if name:
                    supabase.table("vendors").insert({
                        "name": name, 
                        "contact": contact, 
                        "opening_balance": opening
                    }).execute()
                    st.success("Added!")
                    st.rerun()
        vendors = fetch_vendors()
        st.dataframe(vendors[['name', 'contact', 'opening_balance']], use_container_width=True)

# -------------------- Shift Recording Page --------------------
elif st.session_state.page == "Shift Recording":
    st.title("📝 Shift Recording")
    
    # Shift selection buttons
    col1, col2, col3 = st.columns(3)
    with col1:
        if st.button("🌅 Morning Shift", use_container_width=True):
            st.session_state.current_shift_name = "Morning"
            st.session_state.current_date = date.today()
            shift_id = get_or_create_shift(st.session_state.current_date, "Morning")
            st.session_state.current_shift_id = shift_id
            st.rerun()
    with col2:
        if st.button("☀️ Evening Shift", use_container_width=True):
            st.session_state.current_shift_name = "Evening"
            st.session_state.current_date = date.today()
            shift_id = get_or_create_shift(st.session_state.current_date, "Evening")
            st.session_state.current_shift_id = shift_id
            st.rerun()
    with col3:
        if st.button("🌙 Night Shift", use_container_width=True):
            st.session_state.current_shift_name = "Night"
            st.session_state.current_date = date.today()
            shift_id = get_or_create_shift(st.session_state.current_date, "Night")
            st.session_state.current_shift_id = shift_id
            st.rerun()
    
    if st.session_state.current_shift_id:
        shift_id = st.session_state.current_shift_id
        shift_info = supabase.table("shifts").select("*").eq("id", shift_id).execute().data[0]
        
        if shift_info['status'] == 'closed':
            st.warning("This shift is closed. You cannot edit it.")
            if st.button("Clear Selection"):
                st.session_state.current_shift_id = None
                st.rerun()
        else:
            st.subheader(f"Recording {st.session_state.current_shift_name} Shift - {shift_info['date']}")
            
            # Opening cash
            st.metric("Opening Cash", f"₹{shift_info['opening_cash']:,.2f}")
            
            # Total Sale
            new_sale = st.number_input("Total Sale", value=float(shift_info['total_sale'] or 0), min_value=0.0, step=100.0)
            if new_sale != shift_info['total_sale']:
                supabase.table("shifts").update({"total_sale": new_sale}).eq("id", shift_id).execute()
                update_expected_cash(shift_id)
                st.rerun()
            
            st.markdown("---")
            
            # ========== EXPENSES ==========
            st.subheader("Expenses")
            heads_df = fetch_expense_heads()
            head_options = {row['name']: row['id'] for _, row in heads_df.iterrows()}
            
            with st.form("expense_form"):
                cols = st.columns(4)
                with cols[0]:
                    head = st.selectbox("Head", list(head_options.keys()), key="exp_head")
                with cols[1]:
                    amt = st.number_input("Amount", min_value=0.01, step=10.0, key="exp_amt")
                with cols[2]:
                    src = st.selectbox("Source", ["sales_cash", "owner_pocket"], key="exp_src")
                with cols[3]:
                    desc = st.text_input("Description", key="exp_desc")
                if st.form_submit_button("Add Expense"):
                    # Insert expense
                    supabase.table("expenses").insert({
                        "shift_id": shift_id,
                        "expense_head_id": head_options[head],
                        "amount": amt,
                        "source": src,
                        "description": desc
                    }).execute()
                    # If source is owner_pocket, record in owner ledger (owner puts in money)
                    if src == "owner_pocket":
                        record_owner_ledger(amt, f"Expense: {head}", shift_id)
                    update_expected_cash(shift_id)
                    st.success("Expense added")
                    st.rerun()
            
            # Show existing expenses
            expenses = supabase.table("expenses").select("*, expense_heads(name)").eq("shift_id", shift_id).execute()
            if expenses.data:
                df_exp = pd.DataFrame(expenses.data)
                df_exp['head'] = df_exp['expense_heads'].apply(lambda x: x['name'])
                st.dataframe(df_exp[['head', 'amount', 'source', 'description']], use_container_width=True)
            
            st.markdown("---")
            
            # ========== VENDOR PAYMENTS ==========
            st.subheader("Vendor Payments")
            vendors_df = fetch_vendors()
            vendor_options = {row['name']: row['id'] for _, row in vendors_df.iterrows()}
            
            with st.form("payment_form"):
                cols = st.columns(4)
                with cols[0]:
                    vendor = st.selectbox("Vendor", list(vendor_options.keys()), key="pay_vendor")
                with cols[1]:
                    amt = st.number_input("Amount", min_value=0.01, step=10.0, key="pay_amt")
                with cols[2]:
                    src = st.selectbox("Source", ["sales_cash", "owner_pocket"], key="pay_src")
                with cols[3]:
                    desc = st.text_input("Description", key="pay_desc")
                if st.form_submit_button("Add Payment"):
                    supabase.table("vendor_payments").insert({
                        "shift_id": shift_id,
                        "vendor_id": vendor_options[vendor],
                        "amount": amt,
                        "source": src,
                        "description": desc
                    }).execute()
                    if src == "owner_pocket":
                        record_owner_ledger(amt, f"Vendor Payment: {vendor}", shift_id)
                    update_expected_cash(shift_id)
                    st.success("Payment added")
                    st.rerun()
            
            payments = supabase.table("vendor_payments").select("*, vendors(name)").eq("shift_id", shift_id).execute()
            if payments.data:
                df_pay = pd.DataFrame(payments.data)
                df_pay['vendor'] = df_pay['vendors'].apply(lambda x: x['name'])
                st.dataframe(df_pay[['vendor', 'amount', 'source', 'description']], use_container_width=True)
            
            st.markdown("---")
            
            # ========== PURCHASES ==========
            st.subheader("Purchases")
            with st.form("purchase_form"):
                cols = st.columns(5)
                with cols[0]:
                    vendor = st.selectbox("Vendor", list(vendor_options.keys()), key="pur_vendor")
                with cols[1]:
                    amt = st.number_input("Amount", min_value=0.01, step=10.0, key="pur_amt")
                with cols[2]:
                    pay_type = st.selectbox("Payment Type", ["cash", "credit"], key="pur_type")
                with cols[3]:
                    src = st.selectbox("Source if Cash", ["sales_cash", "owner_pocket"], 
                                       disabled=pay_type!="cash", key="pur_src")
                with cols[4]:
                    desc = st.text_input("Description", key="pur_desc")
                if st.form_submit_button("Add Purchase"):
                    data = {
                        "shift_id": shift_id,
                        "vendor_id": vendor_options[vendor],
                        "amount": amt,
                        "payment_type": pay_type,
                        "description": desc
                    }
                    if pay_type == "cash":
                        data["source_if_cash"] = src
                    supabase.table("purchases").insert(data).execute()
                    if pay_type == "cash" and src == "owner_pocket":
                        record_owner_ledger(amt, f"Purchase (cash) from {vendor}", shift_id)
                    if pay_type == "cash" and src == "sales_cash":
                        update_expected_cash(shift_id)
                    st.success("Purchase added")
                    st.rerun()
            
            purchases = supabase.table("purchases").select("*, vendors(name)").eq("shift_id", shift_id).execute()
            if purchases.data:
                df_pur = pd.DataFrame(purchases.data)
                df_pur['vendor'] = df_pur['vendors'].apply(lambda x: x['name'])
                st.dataframe(df_pur[['vendor', 'amount', 'payment_type', 'source_if_cash', 'description']], use_container_width=True)
            
            st.markdown("---")
            
            # ========== RETURNS ==========
            st.subheader("Returns to Vendors")
            with st.form("return_form"):
                cols = st.columns(3)
                with cols[0]:
                    vendor = st.selectbox("Vendor", list(vendor_options.keys()), key="ret_vendor")
                with cols[1]:
                    amt = st.number_input("Amount", min_value=0.01, step=10.0, key="ret_amt")
                with cols[2]:
                    desc = st.text_input("Description", key="ret_desc")
                if st.form_submit_button("Add Return"):
                    supabase.table("returns").insert({
                        "shift_id": shift_id,
                        "vendor_id": vendor_options[vendor],
                        "amount": amt,
                        "description": desc
                    }).execute()
                    st.success("Return added")
                    st.rerun()
            
            returns = supabase.table("returns").select("*, vendors(name)").eq("shift_id", shift_id).execute()
            if returns.data:
                df_ret = pd.DataFrame(returns.data)
                df_ret['vendor'] = df_ret['vendors'].apply(lambda x: x['name'])
                st.dataframe(df_ret[['vendor', 'amount', 'description']], use_container_width=True)
            
            st.markdown("---")
            
            # ========== WITHDRAWALS ==========
            st.subheader("Withdrawals (Owner takes cash)")
            with st.form("withdrawal_form"):
                cols = st.columns(2)
                with cols[0]:
                    amt = st.number_input("Amount", min_value=0.01, step=10.0, key="wd_amt")
                with cols[1]:
                    desc = st.text_input("Description", key="wd_desc")
                if st.form_submit_button("Add Withdrawal"):
                    supabase.table("withdrawals").insert({
                        "shift_id": shift_id,
                        "amount": amt,
                        "description": desc
                    }).execute()
                    # Record in owner ledger as negative (owner withdraws)
                    record_owner_ledger(-amt, f"Withdrawal: {desc}", shift_id)
                    update_expected_cash(shift_id)
                    st.success("Withdrawal added")
                    st.rerun()
            
            withdrawals = supabase.table("withdrawals").select("*").eq("shift_id", shift_id).execute()
            if withdrawals.data:
                df_wd = pd.DataFrame(withdrawals.data)
                st.dataframe(df_wd[['amount', 'description']], use_container_width=True)
            
            st.markdown("---")
            
            # ========== CLOSE SHIFT ==========
            st.subheader("Close Shift")
            expected = calculate_expected_cash(shift_id)
            st.metric("Expected Cash", f"₹{expected:,.2f}")
            closing = st.number_input("Enter Closing Cash", min_value=0.0, step=100.0)
            if st.button("Close Shift"):
                if closing >= 0:
                    close_shift(shift_id, closing)
                    st.success("Shift closed!")
                    st.session_state.current_shift_id = None
                    st.rerun()
                else:
                    st.error("Invalid amount")

# -------------------- Reports Page --------------------
elif st.session_state.page == "Reports":
    st.title("📈 Reports")
    
    report_type = st.selectbox("Report Type", 
                                ["Expense Head Wise", "All Expenses", "Sales Summary", 
                                 "Shift Wise Summary", "Withdrawals", "Vendor Payments", 
                                 "Vendor Ledger", "Owner Ledger"])
    
    # Date range filter
    col1, col2 = st.columns(2)
    with col1:
        start_date = st.date_input("Start Date", value=date.today().replace(day=1))
    with col2:
        end_date = st.date_input("End Date", value=date.today())
    
    # Global search filter
    search_term = st.text_input("🔍 Search (applies to all text columns)")
    
    # Function to filter dataframe based on search
    def filter_df(df, search):
        if search and not df.empty:
            mask = df.astype(str).apply(lambda x: x.str.contains(search, case=False, na=False)).any(axis=1)
            return df[mask]
        return df
    
    # Fetch data based on report type
    if report_type in ["Expense Head Wise", "All Expenses"]:
        # Get shifts in date range
        shifts = supabase.table("shifts").select("id, date, shift_name").gte("date", start_date.isoformat()).lte("date", end_date.isoformat()).execute()
        shift_ids = [s['id'] for s in shifts.data] if shifts.data else []
        if not shift_ids:
            st.info("No data in selected range")
        else:
            expenses = supabase.table("expenses").select("*, expense_heads(name), shifts(date, shift_name)").in_("shift_id", shift_ids).execute()
            if expenses.data:
                df = pd.DataFrame(expenses.data)
                df['head'] = df['expense_heads'].apply(lambda x: x['name'])
                df['date'] = df['shifts'].apply(lambda x: x['date'])
                df['shift'] = df['shifts'].apply(lambda x: x['shift_name'])
                df = df[['date', 'shift', 'head', 'amount', 'source', 'description']]
                
                if report_type == "Expense Head Wise":
                    # Group by head
                    summary = df.groupby('head')['amount'].sum().reset_index()
                    fig = px.bar(summary, x='head', y='amount', title="Expenses by Head")
                    st.plotly_chart(fig)
                    st.dataframe(summary, use_container_width=True)
                else:
                    # All expenses with search
                    df_filtered = filter_df(df, search_term)
                    st.dataframe(df_filtered, use_container_width=True)
            else:
                st.info("No expenses found")
    
    elif report_type == "Sales Summary":
        shifts = supabase.table("shifts").select("date, shift_name, total_sale").gte("date", start_date.isoformat()).lte("date", end_date.isoformat()).execute()
        if shifts.data:
            df = pd.DataFrame(shifts.data)
            df['date'] = pd.to_datetime(df['date'])
            df = df.sort_values('date')
            fig = px.line(df, x='date', y='total_sale', color='shift_name', title="Daily Sales by Shift")
            st.plotly_chart(fig)
            st.dataframe(df, use_container_width=True)
        else:
            st.info("No sales data")
    
    elif report_type == "Shift Wise Summary":
        shifts = supabase.table("shifts").select("*").gte("date", start_date.isoformat()).lte("date", end_date.isoformat()).execute()
        if shifts.data:
            df = pd.DataFrame(shifts.data)
            df = df[['date', 'shift_name', 'opening_cash', 'total_sale', 'expected_cash', 'closing_cash_entered', 'status']]
            df_filtered = filter_df(df, search_term)
            st.dataframe(df_filtered, use_container_width=True)
        else:
            st.info("No shift data")
    
    elif report_type == "Withdrawals":
        shifts = supabase.table("shifts").select("id").gte("date", start_date.isoformat()).lte("date", end_date.isoformat()).execute()
        shift_ids = [s['id'] for s in shifts.data] if shifts.data else []
        if shift_ids:
            wd = supabase.table("withdrawals").select("*, shifts(date, shift_name)").in_("shift_id", shift_ids).execute()
            if wd.data:
                df = pd.DataFrame(wd.data)
                df['date'] = df['shifts'].apply(lambda x: x['date'])
                df['shift'] = df['shifts'].apply(lambda x: x['shift_name'])
                df = df[['date', 'shift', 'amount', 'description']]
                df_filtered = filter_df(df, search_term)
                st.dataframe(df_filtered, use_container_width=True)
            else:
                st.info("No withdrawals")
        else:
            st.info("No data")
    
    elif report_type == "Vendor Payments":
        shifts = supabase.table("shifts").select("id").gte("date", start_date.isoformat()).lte("date", end_date.isoformat()).execute()
        shift_ids = [s['id'] for s in shifts.data] if shifts.data else []
        if shift_ids:
            pays = supabase.table("vendor_payments").select("*, vendors(name), shifts(date, shift_name)").in_("shift_id", shift_ids).execute()
            if pays.data:
                df = pd.DataFrame(pays.data)
                df['vendor'] = df['vendors'].apply(lambda x: x['name'])
                df['date'] = df['shifts'].apply(lambda x: x['date'])
                df['shift'] = df['shifts'].apply(lambda x: x['shift_name'])
                df = df[['date', 'shift', 'vendor', 'amount', 'source', 'description']]
                df_filtered = filter_df(df, search_term)
                st.dataframe(df_filtered, use_container_width=True)
            else:
                st.info("No payments")
        else:
            st.info("No data")
    
    elif report_type == "Vendor Ledger":
        vendors_df = fetch_vendors()
        vendor_options = {row['name']: row['id'] for _, row in vendors_df.iterrows()}
        vendor = st.selectbox("Select Vendor", list(vendor_options.keys()))
        
        if st.button("Generate Ledger"):
            vendor_id = vendor_options[vendor]
            
            # Get opening balance
            vendor_info = supabase.table("vendors").select("opening_balance").eq("id", vendor_id).execute().data[0]
            opening = vendor_info['opening_balance'] or 0
            
            # Fetch all transactions for this vendor within date range
            purchases = supabase.table("purchases").select("amount, payment_type, created_at, description").eq("vendor_id", vendor_id).gte("created_at", start_date.isoformat()).lte("created_at", end_date.isoformat()).execute()
            payments = supabase.table("vendor_payments").select("amount, source, created_at, description").eq("vendor_id", vendor_id).gte("created_at", start_date.isoformat()).lte("created_at", end_date.isoformat()).execute()
            returns = supabase.table("returns").select("amount, created_at, description").eq("vendor_id", vendor_id).gte("created_at", start_date.isoformat()).lte("created_at", end_date.isoformat()).execute()
            
            # Build ledger with running balance
            ledger = []
            balance = opening
            
            # Add opening row
            ledger.append({
                "date": start_date.isoformat(),
                "type": "Opening Balance",
                "debit": 0,
                "credit": 0,
                "balance": balance,
                "description": "Opening"
            })
            
            for p in purchases.data:
                amount = p['amount']
                if p['payment_type'] == 'credit':
                    balance += amount  # credit purchase increases what we owe (liability)
                    ledger.append({
                        "date": p['created_at'],
                        "type": "Credit Purchase",
                        "debit": amount,
                        "credit": 0,
                        "balance": balance,
                        "description": p.get('description','')
                    })
                else:  # cash purchase - no ledger effect except if credit? Actually cash purchase doesn't change vendor balance, but we may want to show it for completeness.
                    # For vendor ledger, cash purchase doesn't affect balance (paid immediately)
                    ledger.append({
                        "date": p['created_at'],
                        "type": "Cash Purchase",
                        "debit": 0,
                        "credit": 0,
                        "balance": balance,
                        "description": p.get('description','') + " (cash)"
                    })
            
            for p in payments.data:
                amount = p['amount']
                balance -= amount  # payment reduces what we owe
                ledger.append({
                    "date": p['created_at'],
                    "type": "Payment",
                    "debit": 0,
                    "credit": amount,
                    "balance": balance,
                    "description": p.get('description','')
                })
            
            for r in returns.data:
                amount = r['amount']
                balance -= amount  # return reduces what we owe
                ledger.append({
                    "date": r['created_at'],
                    "type": "Return",
                    "debit": 0,
                    "credit": amount,
                    "balance": balance,
                    "description": r.get('description','')
                })
            
            df = pd.DataFrame(ledger)
            if not df.empty:
                df = df.sort_values('date')
                st.dataframe(df, use_container_width=True)
                
                # PDF download (simple)
                if st.button("Download PDF"):
                    pdf = FPDF()
                    pdf.add_page()
                    pdf.set_font("Arial", size=10)
                    pdf.cell(200, 10, txt=f"Vendor Ledger: {vendor}", ln=1, align='C')
                    pdf.ln(5)
                    # Simple table
                    cols = df.columns.tolist()
                    for col in cols:
                        pdf.cell(40, 8, col, border=1)
                    pdf.ln()
                    for _, row in df.iterrows():
                        for val in row:
                            pdf.cell(40, 8, str(val)[:20], border=1)
                        pdf.ln()
                    pdf_output = pdf.output(dest='S').encode('latin1')
                    b64 = base64.b64encode(pdf_output).decode()
                    href = f'<a href="data:application/octet-stream;base64,{b64}" download="ledger.pdf">Download PDF</a>'
                    st.markdown(href, unsafe_allow_html=True)
            else:
                st.info("No transactions")
    
    elif report_type == "Owner Ledger":
        if st.button("Generate Owner Ledger"):
            # Fetch all owner transactions within date range
            owner_tx = supabase.table("owner_ledger").select("*").gte("transaction_date", start_date.isoformat()).lte("transaction_date", end_date.isoformat()).order("transaction_date").execute()
            if owner_tx.data:
                df = pd.DataFrame(owner_tx.data)
                df['running_balance'] = df['amount'].cumsum()
                st.dataframe(df[['transaction_date', 'amount', 'description', 'running_balance']], use_container_width=True)
            else:
                st.info("No owner transactions")
