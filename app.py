import streamlit as st
import pandas as pd
from datetime import datetime, date
import os
from supabase import create_client, Client
import plotly.express as px
from io import BytesIO
from fpdf import FPDF
import base64

# -------------------- Supabase Initialization --------------------
SUPABASE_URL = st.secrets["SUPABASE_URL"]
SUPABASE_KEY = st.secrets["SUPABASE_KEY"]
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# -------------------- Session State Initialization --------------------
if 'authenticated' not in st.session_state:
    st.session_state.authenticated = False
if 'current_shift_id' not in st.session_state:
    st.session_state.current_shift_id = None
if 'current_shift_name' not in st.session_state:
    st.session_state.current_shift_name = None
if 'current_date' not in st.session_state:
    st.session_state.current_date = date.today()

# -------------------- Authentication (simple) --------------------
def login():
    st.title("🔐 Medical Store Login")
    password = st.text_input("Enter Password", type="password")
    if st.button("Login"):
        # Use environment variable or hardcoded password
        if password == st.secrets.get("APP_PASSWORD", "admin123"):
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("Incorrect password")

if not st.session_state.authenticated:
    login()
    st.stop()

# -------------------- Helper Functions --------------------
def fetch_expense_heads():
    response = supabase.table("expense_heads").select("*").execute()
    return pd.DataFrame(response.data)

def fetch_vendors():
    response = supabase.table("vendors").select("*").execute()
    return pd.DataFrame(response.data)

def fetch_shifts(date_selected):
    response = supabase.table("shifts").select("*").eq("date", date_selected).execute()
    return pd.DataFrame(response.data)

def get_or_create_shift(date_selected, shift_name):
    # Check if shift exists
    response = supabase.table("shifts").select("*").eq("date", date_selected).eq("shift_name", shift_name).execute()
    if response.data:
        return response.data[0]['id']
    else:
        # Determine opening cash: closing of previous shift or 0
        prev_shifts = supabase.table("shifts").select("*").eq("date", date_selected).lt("shift_name", shift_name).order("shift_name").execute()
        opening = 0
        if prev_shifts.data:
            last_shift = prev_shifts.data[-1]
            opening = last_shift.get('expected_cash') or last_shift.get('closing_cash_entered') or 0
        # Create shift
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
    # Get shift details
    shift = supabase.table("shifts").select("*").eq("id", shift_id).execute().data[0]
    opening = shift['opening_cash'] or 0
    sale = shift['total_sale'] or 0
    
    # Expenses from sales cash
    expenses_sales = supabase.table("expenses").select("amount").eq("shift_id", shift_id).eq("source", "sales_cash").execute()
    total_expenses_sales = sum([e['amount'] for e in expenses_sales.data])
    
    # Vendor payments from sales cash
    payments_sales = supabase.table("vendor_payments").select("amount").eq("shift_id", shift_id).eq("source", "sales_cash").execute()
    total_payments_sales = sum([p['amount'] for p in payments_sales.data])
    
    # Purchases cash from sales cash
    purchases_sales = supabase.table("purchases").select("amount").eq("shift_id", shift_id).eq("payment_type", "cash").eq("source_if_cash", "sales_cash").execute()
    total_purchases_sales = sum([p['amount'] for p in purchases_sales.data])
    
    # Withdrawals
    withdrawals = supabase.table("withdrawals").select("amount").eq("shift_id", shift_id).execute()
    total_withdrawals = sum([w['amount'] for w in withdrawals.data])
    
    expected = opening + sale - (total_expenses_sales + total_payments_sales + total_purchases_sales + total_withdrawals)
    return expected

def update_expected_cash(shift_id):
    expected = calculate_expected_cash(shift_id)
    supabase.table("shifts").update({"expected_cash": expected}).eq("id", shift_id).execute()

def close_shift(shift_id, closing_cash):
    shift = supabase.table("shifts").select("*").eq("id", shift_id).execute().data[0]
    expected = shift['expected_cash'] or calculate_expected_cash(shift_id)
    if closing_cash < expected:
        # Create shortage expense
        shortage_head = supabase.table("expense_heads").select("id").eq("name", "Cash Shortage").execute().data[0]['id']
        shortage_amount = expected - closing_cash
        supabase.table("expenses").insert({
            "shift_id": shift_id,
            "expense_head_id": shortage_head,
            "amount": shortage_amount,
            "source": "sales_cash",
            "description": "Auto-recorded cash shortage"
        }).execute()
        # Update expected after adding shortage
        update_expected_cash(shift_id)
    # Close shift
    supabase.table("shifts").update({
        "closing_cash_entered": closing_cash,
        "status": "closed",
        "closed_at": datetime.now().isoformat()
    }).eq("id", shift_id).execute()

# -------------------- Page Layout --------------------
st.set_page_config(page_title="Medical Store 3-Shift Accounting", layout="wide")

tabs = st.tabs(["📊 Dashboard", "⚙️ Heads Setup", "📝 Shift Recording", "📈 Reports"])

# -------------------- Dashboard Tab --------------------
with tabs[0]:
    st.header("Dashboard")
    col1, col2 = st.columns([1, 3])
    with col1:
        selected_date = st.date_input("Select Date", value=date.today())
    with col2:
        st.subheader(f"Summary for {selected_date}")
    
    # Fetch all shifts for that date
    shifts = fetch_shifts(selected_date)
    if shifts.empty:
        st.info("No shifts recorded for this date.")
    else:
        # Aggregate data across shifts
        total_sale = shifts['total_sale'].sum()
        total_expenses = supabase.table("expenses").select("amount").in_("shift_id", shifts['id'].tolist()).execute()
        total_expenses = sum([e['amount'] for e in total_expenses.data]) if total_expenses.data else 0
        total_withdrawals = supabase.table("withdrawals").select("amount").in_("shift_id", shifts['id'].tolist()).execute()
        total_withdrawals = sum([w['amount'] for w in total_withdrawals.data]) if total_withdrawals.data else 0
        total_payments = supabase.table("vendor_payments").select("amount").in_("shift_id", shifts['id'].tolist()).execute()
        total_payments = sum([p['amount'] for p in total_payments.data]) if total_payments.data else 0
        
        # Overall cash available: sum of closing cash of last shift? Or just expected of last shift?
        last_shift = shifts.iloc[-1]
        available_cash = last_shift['expected_cash'] if pd.notna(last_shift['expected_cash']) else 0
        
        cola, colb, colc, cold, cole = st.columns(5)
        cola.metric("Total Sale", f"₹{total_sale:,.2f}")
        colb.metric("Expenses", f"₹{total_expenses:,.2f}")
        colc.metric("Withdrawals", f"₹{total_withdrawals:,.2f}")
        cold.metric("Vendor Payments", f"₹{total_payments:,.2f}")
        cole.metric("Available Cash", f"₹{available_cash:,.2f}")
        
        # Show shifts breakdown
        st.subheader("Shifts Breakdown")
        for _, shift in shifts.iterrows():
            with st.expander(f"{shift['shift_name']} Shift - {'Closed' if shift['status']=='closed' else 'Open'}"):
                st.write(f"Opening Cash: ₹{shift['opening_cash']:,.2f}")
                st.write(f"Total Sale: ₹{shift['total_sale']:,.2f}")
                st.write(f"Expected Cash: ₹{shift['expected_cash']:,.2f}" if pd.notna(shift['expected_cash']) else "Expected Cash: Not calculated")
                if pd.notna(shift['closing_cash_entered']):
                    st.write(f"Closing Cash Entered: ₹{shift['closing_cash_entered']:,.2f}")

# -------------------- Heads Setup Tab --------------------
with tabs[1]:
    st.header("Expense Heads & Vendors")
    
    tab1, tab2 = st.tabs(["Expense Heads", "Vendors"])
    
    with tab1:
        st.subheader("Manage Expense Heads")
        with st.form("add_expense_head"):
            name = st.text_input("Head Name")
            description = st.text_area("Description (optional)")
            if st.form_submit_button("Add Head"):
                if name:
                    supabase.table("expense_heads").insert({"name": name, "description": description}).execute()
                    st.success("Added!")
                    st.rerun()
        # Show existing
        heads = fetch_expense_heads()
        st.dataframe(heads[['name', 'description']])
    
    with tab2:
        st.subheader("Manage Vendors")
        with st.form("add_vendor"):
            name = st.text_input("Vendor Name")
            contact = st.text_input("Contact (optional)")
            opening_balance = st.number_input("Opening Balance", value=0.0)
            if st.form_submit_button("Add Vendor"):
                if name:
                    supabase.table("vendors").insert({"name": name, "contact": contact, "opening_balance": opening_balance}).execute()
                    st.success("Added!")
                    st.rerun()
        vendors = fetch_vendors()
        st.dataframe(vendors[['name', 'contact', 'opening_balance']])

# -------------------- Shift Recording Tab --------------------
with tabs[2]:
    st.header("Shift Recording")
    
    col1, col2, col3 = st.columns(3)
    with col1:
        if st.button("🌅 Morning Shift", use_container_width=True):
            st.session_state.current_shift_name = "Morning"
            st.session_state.current_date = st.date_input("Select Date", value=date.today(), key="morning_date")
            shift_id = get_or_create_shift(st.session_state.current_date, "Morning")
            st.session_state.current_shift_id = shift_id
            st.rerun()
    with col2:
        if st.button("☀️ Evening Shift", use_container_width=True):
            st.session_state.current_shift_name = "Evening"
            st.session_state.current_date = st.date_input("Select Date", value=date.today(), key="evening_date")
            shift_id = get_or_create_shift(st.session_state.current_date, "Evening")
            st.session_state.current_shift_id = shift_id
            st.rerun()
    with col3:
        if st.button("🌙 Night Shift", use_container_width=True):
            st.session_state.current_shift_name = "Night"
            st.session_state.current_date = st.date_input("Select Date", value=date.today(), key="night_date")
            shift_id = get_or_create_shift(st.session_state.current_date, "Night")
            st.session_state.current_shift_id = shift_id
            st.rerun()
    
    if st.session_state.current_shift_id:
        shift_id = st.session_state.current_shift_id
        shift_info = supabase.table("shifts").select("*").eq("id", shift_id).execute().data[0]
        if shift_info['status'] == 'closed':
            st.warning("This shift is already closed. You cannot edit it.")
            if st.button("Clear Selection"):
                st.session_state.current_shift_id = None
                st.rerun()
        else:
            st.subheader(f"Recording {st.session_state.current_shift_name} Shift - {shift_info['date']}")
            
            # Opening cash display (read-only)
            st.metric("Opening Cash", f"₹{shift_info['opening_cash']:,.2f}")
            
            # Total Sale
            new_sale = st.number_input("Total Sale", value=float(shift_info['total_sale'] or 0), min_value=0.0, step=100.0)
            if new_sale != shift_info['total_sale']:
                supabase.table("shifts").update({"total_sale": new_sale}).eq("id", shift_id).execute()
                update_expected_cash(shift_id)
                st.rerun()
            
            st.markdown("---")
            
            # Expenses Section
            st.subheader("Expenses")
            heads = fetch_expense_heads()
            head_options = {row['name']: row['id'] for _, row in heads.iterrows()}
            with st.form("add_expense"):
                cola, colb, colc, cold = st.columns(4)
                with cola:
                    head_name = st.selectbox("Expense Head", list(head_options.keys()))
                with colb:
                    amount = st.number_input("Amount", min_value=0.01, step=10.0)
                with colc:
                    source = st.selectbox("Source", ["sales_cash", "owner_pocket"])
                with cold:
                    desc = st.text_input("Description (optional)")
                submitted = st.form_submit_button("Add Expense")
                if submitted:
                    supabase.table("expenses").insert({
                        "shift_id": shift_id,
                        "expense_head_id": head_options[head_name],
                        "amount": amount,
                        "source": source,
                        "description": desc
                    }).execute()
                    update_expected_cash(shift_id)
                    st.success("Expense added")
                    st.rerun()
            # Show existing expenses
            expenses = supabase.table("expenses").select("*, expense_heads(name)").eq("shift_id", shift_id).execute()
            if expenses.data:
                df_exp = pd.DataFrame(expenses.data)
                df_exp['head'] = df_exp['expense_heads'].apply(lambda x: x['name'])
                st.dataframe(df_exp[['head', 'amount', 'source', 'description']])
            
            st.markdown("---")
            
            # Vendor Payments Section
            st.subheader("Vendor Payments")
            vendors_df = fetch_vendors()
            vendor_options = {row['name']: row['id'] for _, row in vendors_df.iterrows()}
            with st.form("add_payment"):
                cola, colb, colc, cold = st.columns(4)
                with cola:
                    vendor_name = st.selectbox("Vendor", list(vendor_options.keys()))
                with colb:
                    amount = st.number_input("Amount", min_value=0.01, step=10.0, key="pay_amount")
                with colc:
                    source = st.selectbox("Source", ["sales_cash", "owner_pocket"], key="pay_source")
                with cold:
                    desc = st.text_input("Description", key="pay_desc")
                submitted = st.form_submit_button("Add Payment")
                if submitted:
                    supabase.table("vendor_payments").insert({
                        "shift_id": shift_id,
                        "vendor_id": vendor_options[vendor_name],
                        "amount": amount,
                        "source": source,
                        "description": desc
                    }).execute()
                    update_expected_cash(shift_id)
                    st.success("Payment added")
                    st.rerun()
            # Show existing payments
            payments = supabase.table("vendor_payments").select("*, vendors(name)").eq("shift_id", shift_id).execute()
            if payments.data:
                df_pay = pd.DataFrame(payments.data)
                df_pay['vendor'] = df_pay['vendors'].apply(lambda x: x['name'])
                st.dataframe(df_pay[['vendor', 'amount', 'source', 'description']])
            
            st.markdown("---")
            
            # Purchases Section
            st.subheader("Purchases")
            with st.form("add_purchase"):
                cola, colb, colc, cold, cole = st.columns(5)
                with cola:
                    vendor_name = st.selectbox("Vendor", list(vendor_options.keys()), key="pur_vendor")
                with colb:
                    amount = st.number_input("Amount", min_value=0.01, step=10.0, key="pur_amount")
                with colc:
                    payment_type = st.selectbox("Payment Type", ["cash", "credit"])
                with cold:
                    source_if_cash = st.selectbox("Source if Cash", ["sales_cash", "owner_pocket"], disabled=payment_type!="cash")
                with cole:
                    desc = st.text_input("Description", key="pur_desc")
                submitted = st.form_submit_button("Add Purchase")
                if submitted:
                    data = {
                        "shift_id": shift_id,
                        "vendor_id": vendor_options[vendor_name],
                        "amount": amount,
                        "payment_type": payment_type,
                        "description": desc
                    }
                    if payment_type == "cash":
                        data["source_if_cash"] = source_if_cash
                    supabase.table("purchases").insert(data).execute()
                    if payment_type == "cash" and source_if_cash == "sales_cash":
                        update_expected_cash(shift_id)
                    st.success("Purchase added")
                    st.rerun()
            # Show purchases
            purchases = supabase.table("purchases").select("*, vendors(name)").eq("shift_id", shift_id).execute()
            if purchases.data:
                df_pur = pd.DataFrame(purchases.data)
                df_pur['vendor'] = df_pur['vendors'].apply(lambda x: x['name'])
                st.dataframe(df_pur[['vendor', 'amount', 'payment_type', 'source_if_cash', 'description']])
            
            st.markdown("---")
            
            # Returns Section
            st.subheader("Returns to Vendors")
            with st.form("add_return"):
                cola, colb, colc = st.columns(3)
                with cola:
                    vendor_name = st.selectbox("Vendor", list(vendor_options.keys()), key="ret_vendor")
                with colb:
                    amount = st.number_input("Amount", min_value=0.01, step=10.0, key="ret_amount")
                with colc:
                    desc = st.text_input("Description", key="ret_desc")
                submitted = st.form_submit_button("Add Return")
                if submitted:
                    supabase.table("returns").insert({
                        "shift_id": shift_id,
                        "vendor_id": vendor_options[vendor_name],
                        "amount": amount,
                        "description": desc
                    }).execute()
                    # Returns don't affect cash, but update vendor ledger only
                    st.success("Return added")
                    st.rerun()
            # Show returns
            returns = supabase.table("returns").select("*, vendors(name)").eq("shift_id", shift_id).execute()
            if returns.data:
                df_ret = pd.DataFrame(returns.data)
                df_ret['vendor'] = df_ret['vendors'].apply(lambda x: x['name'])
                st.dataframe(df_ret[['vendor', 'amount', 'description']])
            
            st.markdown("---")
            
            # Withdrawals Section
            st.subheader("Withdrawals")
            with st.form("add_withdrawal"):
                cola, colb = st.columns(2)
                with cola:
                    amount = st.number_input("Amount", min_value=0.01, step=10.0, key="with_amount")
                with colb:
                    desc = st.text_input("Description", key="with_desc")
                submitted = st.form_submit_button("Add Withdrawal")
                if submitted:
                    supabase.table("withdrawals").insert({
                        "shift_id": shift_id,
                        "amount": amount,
                        "description": desc
                    }).execute()
                    update_expected_cash(shift_id)
                    st.success("Withdrawal added")
                    st.rerun()
            # Show withdrawals
            withdrawals = supabase.table("withdrawals").select("*").eq("shift_id", shift_id).execute()
            if withdrawals.data:
                df_with = pd.DataFrame(withdrawals.data)
                st.dataframe(df_with[['amount', 'description']])
            
            st.markdown("---")
            
            # Closing Cash
            st.subheader("Close Shift")
            expected = calculate_expected_cash(shift_id)
            st.metric("Expected Cash", f"₹{expected:,.2f}")
            closing = st.number_input("Enter Closing Cash", min_value=0.0, step=100.0, key="closing_cash")
            if st.button("Close Shift"):
                if closing >= 0:
                    close_shift(shift_id, closing)
                    st.success("Shift closed!")
                    st.session_state.current_shift_id = None
                    st.rerun()
                else:
                    st.error("Please enter a valid amount")

# -------------------- Reports Tab --------------------
with tabs[3]:
    st.header("Reports")
    
    report_type = st.selectbox("Select Report Type", 
                                ["Expense Head Wise", "All Expenses", "Sales Summary", 
                                 "Shift Wise", "Withdrawals", "Payments", "Vendor Ledger"])
    
    if report_type == "Expense Head Wise":
        st.subheader("Expense Head Wise Report")
        start_date = st.date_input("Start Date", value=date.today().replace(day=1))
        end_date = st.date_input("End Date", value=date.today())
        if st.button("Generate"):
            # Get shifts in date range
            shifts = supabase.table("shifts").select("id").gte("date", start_date).lte("date", end_date).execute()
            shift_ids = [s['id'] for s in shifts.data]
            if shift_ids:
                expenses = supabase.table("expenses").select("amount, expense_heads(name)").in_("shift_id", shift_ids).execute()
                df = pd.DataFrame(expenses.data)
                df['head'] = df['expense_heads'].apply(lambda x: x['name'])
                summary = df.groupby('head')['amount'].sum().reset_index()
                fig = px.bar(summary, x='head', y='amount', title="Expenses by Head")
                st.plotly_chart(fig)
                st.dataframe(summary)
            else:
                st.info("No data in selected range")
    
    elif report_type == "All Expenses":
        st.subheader("All Expenses Report")
        start_date = st.date_input("Start Date", value=date.today().replace(day=1))
        end_date = st.date_input("End Date", value=date.today())
        if st.button("Generate"):
            shifts = supabase.table("shifts").select("id, date, shift_name").gte("date", start_date).lte("date", end_date).execute()
            shift_ids = [s['id'] for s in shifts.data]
            if shift_ids:
                expenses = supabase.table("expenses").select("*, expense_heads(name), shifts(date, shift_name)").in_("shift_id", shift_ids).execute()
                df = pd.DataFrame(expenses.data)
                df['head'] = df['expense_heads'].apply(lambda x: x['name'])
                df['date'] = df['shifts'].apply(lambda x: x['date'])
                df['shift'] = df['shifts'].apply(lambda x: x['shift_name'])
                st.dataframe(df[['date', 'shift', 'head', 'amount', 'source', 'description']])
            else:
                st.info("No data")
    
    elif report_type == "Vendor Ledger":
        st.subheader("Vendor Ledger")
        vendors_df = fetch_vendors()
        vendor_options = {row['name']: row['id'] for _, row in vendors_df.iterrows()}
        vendor = st.selectbox("Select Vendor", list(vendor_options.keys()))
        start_date = st.date_input("Start Date", value=date.today().replace(day=1))
        end_date = st.date_input("End Date", value=date.today())
        if st.button("Generate"):
            vendor_id = vendor_options[vendor]
            # Fetch all transactions for this vendor: purchases, payments, returns
            purchases = supabase.table("purchases").select("amount, payment_type, created_at, description").eq("vendor_id", vendor_id).gte("created_at", start_date.isoformat()).lte("created_at", end_date.isoformat()).execute()
            payments = supabase.table("vendor_payments").select("amount, source, created_at, description").eq("vendor_id", vendor_id).gte("created_at", start_date.isoformat()).lte("created_at", end_date.isoformat()).execute()
            returns = supabase.table("returns").select("amount, created_at, description").eq("vendor_id", vendor_id).gte("created_at", start_date.isoformat()).lte("created_at", end_date.isoformat()).execute()
            
            # Combine into ledger with running balance
            ledger = []
            for p in purchases.data:
                ledger.append({
                    "date": p['created_at'],
                    "type": "Purchase",
                    "debit": p['amount'],
                    "credit": 0,
                    "payment_type": p['payment_type'],
                    "description": p.get('description','')
                })
            for p in payments.data:
                ledger.append({
                    "date": p['created_at'],
                    "type": "Payment",
                    "debit": 0,
                    "credit": p['amount'],
                    "source": p['source'],
                    "description": p.get('description','')
                })
            for r in returns.data:
                ledger.append({
                    "date": r['created_at'],
                    "type": "Return",
                    "debit": 0,
                    "credit": r['amount'],
                    "description": r.get('description','')
                })
            df = pd.DataFrame(ledger)
            if not df.empty:
                df = df.sort_values('date')
                df['balance'] = (df['debit'] - df['credit']).cumsum()
                st.dataframe(df)
            else:
                st.info("No transactions")
    
    # PDF download function (simplified)
    def create_pdf(df, title):
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Arial", size=12)
        pdf.cell(200, 10, txt=title, ln=1, align='C')
        pdf.ln(10)
        # Simple table
        col_width = pdf.w / (len(df.columns) + 1)
        for col in df.columns:
            pdf.cell(col_width, 10, str(col), border=1)
        pdf.ln()
        for _, row in df.iterrows():
            for item in row:
                pdf.cell(col_width, 10, str(item), border=1)
            pdf.ln()
        return pdf.output(dest='S').encode('latin1')
    
    if st.button("Download as PDF"):
        # Dummy implementation - in real code, you'd generate based on current report
        st.info("PDF generation ready - implement based on report data")
