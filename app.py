from flask import Flask, flash, render_template, request, redirect, url_for, session
from datetime import datetime
import sqlite3
import pandas as pd
import requests
import json
import re
import time 
from io import BytesIO
from flask import send_file


# TEMPLATES = {
#     "order_confirmation": {
#         "subject": "Order Confirmation - {{order_id}}",
#         "body": "Hello {{customer_name}},\n\nYour order with ID {{order_id}} has been confirmed. Thank you for shopping with us!"
#     },
#     "shipping_update": {
#         "subject": "Shipping Update - {{order_id}}",
#         "body": "Hello {{customer_name}},\n\nYour order with ID {{order_id}} has been shipped and is on its way!"
#     },
#     "custom_three_var": {
#         "subject": "Special Offer for {{customer_name}} - Code {{promo_code}}",
#         "body": "Hello {{customer_name}},\n\nWe’re excited to share a special offer just for you!\n\nOffer: {{offer_details}}\nPromo Code: {{promo_code}}\n\nEnjoy your shopping!"
#     }
# }


app = Flask(__name__)
app.secret_key = 'my secret key'  # Important for session security


# Add this near your other template filters (after app creation)
def get_template_variables(template_body):
    return re.findall(r'{{\s*(\w+)\s*}}', template_body)

# Register the function as a template global
@app.context_processor
def utility_processor():
    return dict(get_template_variables=get_template_variables)


@app.template_filter('regex_findall')
def regex_findall(s, pattern):
    return re.findall(pattern, s)

def clean_param(text):
    # Replace newlines and tabs with a space
    text = text.replace("\n", " ").replace("\t", " ")
    # Collapse multiple spaces to a maximum of 4
    text = re.sub(r' {5,}', '    ', text)
    return text.strip()

def init_db():

    with sqlite3.connect('database.db') as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS user_types (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type_name TEXT UNIQUE NOT NULL,
                description TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Add default types if table is empty
        if conn.execute('SELECT COUNT(*) FROM user_types').fetchone()[0] == 0:
            default_types = [
                ('regular', 'Regular users'),
                ('vip', 'VIP customers'),
                ('test', 'Test accounts'),
                ('inactive', 'Inactive users')
            ]
            conn.executemany('''
                INSERT INTO user_types (type_name, description)
                VALUES (?, ?)
            ''', default_types)

    with sqlite3.connect('database.db') as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT NOT NULL UNIQUE,
                order_id TEXT,
                offer_details TEXT,
                user_type TEXT NOT NULL DEFAULT 'regular',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')


    with sqlite3.connect('database.db') as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS sent_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user TEXT,
                phone TEXT,
                message TEXT,
                message_id TEXT UNIQUE,
                status TEXT,
                delivery_status TEXT,
                timestamp TEXT
            )
        ''')

        conn.execute('''
            CREATE TABLE IF NOT EXISTS whatsapp_numbers (
                phone_number_id TEXT PRIMARY KEY,
                verified_name TEXT,
                code_verification_status TEXT,
                token TEXT
            )
        ''')
        
        conn.execute('''
            CREATE TABLE IF NOT EXISTS templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE,
                subject TEXT,
                body TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')

def get_template_variables(template_body):
    return re.findall(r'{{\s*(\w+)\s*}}', template_body)

# Helper function to get all templates from database
def get_all_templates():
    with sqlite3.connect('database.db') as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute('SELECT * FROM templates ORDER BY name')
        return cursor.fetchall()

# Helper function to get a single template by ID
def get_template(template_id):
    with sqlite3.connect('database.db') as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute('SELECT * FROM templates WHERE id = ?', (template_id,))
        return cursor.fetchone()

@app.route('/', methods=['GET', 'POST'])
def index():
    init_db()
    
    # Get all verified numbers from database
    with sqlite3.connect('database.db') as conn:
        cursor = conn.execute('''
            SELECT phone_number_id, verified_name, code_verification_status 
            FROM whatsapp_numbers
            WHERE code_verification_status IN ('APPROVED', 'EXPIRED')
            ORDER BY verified_name
        ''')
        verified_numbers = cursor.fetchall()

    # Handle new number verification
    if request.method == 'POST' and 'waba_id' in request.form:
        waba_id = request.form['waba_id']
        phone_number_id = request.form['phone_number_id']
        access_token = request.form['access_token']

        try:
            # Verify with Facebook API
            url = f'https://graph.facebook.com/v23.0/{waba_id}/phone_numbers'
            params = {'access_token': access_token}
            response = requests.get(url, params=params)
            data = response.json()

            for number in data.get('data', []):
                if number['id'] == phone_number_id:
                    status = number.get('code_verification_status', 'UNKNOWN')
                    
                    # Store in database
                    with sqlite3.connect('database.db') as conn:
                        conn.execute('''
                            INSERT OR REPLACE INTO whatsapp_numbers
                            (phone_number_id, verified_name, code_verification_status, token)
                            VALUES (?, ?, ?, ?)
                        ''', (
                            phone_number_id,
                            number.get('verified_name'),
                            status,
                            access_token
                        ))
                    
                    # Store in session and redirect
                    session['selected_number'] = phone_number_id
                    flash(f'Successfully verified number! Status: {status}', 'success')
                    return redirect(url_for('send_template'))

            flash('Phone number not found in your WABA', 'error')
        
        except Exception as e:
            flash(f'Verification failed: {str(e)}', 'error')

    # Handle selection of existing number
    elif request.method == 'POST' and 'selected_number' in request.form:
        session['selected_number'] = request.form['selected_number']
        return redirect(url_for('send_template'))

    return render_template('index.html', verified_numbers=verified_numbers)

@app.route('/templates', methods=['GET', 'POST'])
def manage_templates():
    if request.method == 'POST':
        # Handle template creation or update
        name = request.form.get('name')
        subject = request.form.get('subject')
        body = request.form.get('body')
        
        if not name or not body:
            flash('Template name and body are required', 'error')
            return redirect(url_for('manage_templates'))
        
        try:
            with sqlite3.connect('database.db') as conn:
                if 'template_id' in request.form:
                    # Update existing template
                    template_id = request.form['template_id']
                    conn.execute('''
                        UPDATE templates 
                        SET name = ?, subject = ?, body = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    ''', (name, subject, body, template_id))
                    flash('Template updated successfully', 'success')
                else:
                    # Create new template
                    conn.execute('''
                        INSERT INTO templates (name, subject, body)
                        VALUES (?, ?, ?)
                    ''', (name, subject, body))
                    flash('Template created successfully', 'success')
        except sqlite3.IntegrityError:
            flash('Template with this name already exists', 'error')
    
    templates = get_all_templates()
    return render_template('manage_templates.html', templates=templates)

@app.route('/template/create', methods=['GET', 'POST'])
def create_template():
    if request.method == 'POST':
        name = request.form.get('name')
        subject = request.form.get('subject')
        body = request.form.get('body')
        
        if not name or not body:
            flash('Template name and body are required', 'error')
            return redirect(url_for('create_template'))
        
        try:
            with sqlite3.connect('database.db') as conn:
                conn.execute('''
                    INSERT INTO templates (name, subject, body)
                    VALUES (?, ?, ?)
                ''', (name, subject, body))
            flash('Template created successfully', 'success')
            return redirect(url_for('manage_templates'))
        except sqlite3.IntegrityError:
            flash('Template with this name already exists', 'error')
            return redirect(url_for('create_template'))
    
    return render_template('create_template.html')




@app.route('/template/edit/<int:template_id>', methods=['GET'])
def edit_template(template_id):
    template = get_template(template_id)
    if not template:
        flash('Template not found', 'error')
        return redirect(url_for('manage_templates'))
    return render_template('edit_template.html', template=template)

@app.route('/template/delete/<int:template_id>', methods=['POST'])
def delete_template(template_id):
    try:
        with sqlite3.connect('database.db') as conn:
            conn.execute('DELETE FROM templates WHERE id = ?', (template_id,))
        flash('Template deleted successfully', 'success')
    except Exception as e:
        flash(f'Error deleting template: {str(e)}', 'error')
    return redirect(url_for('manage_templates'))

@app.route('/send_template', methods=['GET', 'POST'])
def send_template():
    # Verify we have a selected number
    if 'selected_number' not in session:
        flash('No WhatsApp number selected', 'error')
        return redirect(url_for('index'))
    
    # Get access token for selected number
    with sqlite3.connect('database.db') as conn:
        cursor = conn.execute('''
            SELECT token FROM whatsapp_numbers 
            WHERE phone_number_id = ?
        ''', (session['selected_number'],))
        result = cursor.fetchone()
        
    if not result:
        flash('Access token not found for selected number', 'error')
        return redirect(url_for('index'))
    
    access_token = result[0]

    # Get all distinct user types with counts
    with sqlite3.connect('database.db') as conn:
        conn.row_factory = sqlite3.Row
        user_types = conn.execute('''
            SELECT user_type, COUNT(*) as user_count 
            FROM users 
            GROUP BY user_type
        ''').fetchall()
        
        total_users = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]

    # Handle template selection (GET request)
    if request.method == 'GET' and 'template_id' in request.args:
        template_id = request.args.get('template_id')
        selected_template = get_template(template_id)
        
        if not selected_template:
            flash('Invalid template selected', 'error')
            return redirect(url_for('send_template'))
        
        placeholders = get_template_variables(selected_template['body'])
        
        # Get sample user fields from database
        with sqlite3.connect('database.db') as conn:
            conn.row_factory = sqlite3.Row
            sample_user = conn.execute('SELECT * FROM users LIMIT 1').fetchone()
            sample_user_fields = list(sample_user.keys()) if sample_user else []
        
        return render_template('map_fields.html',
                           template=selected_template,
                           template_id=template_id,
                           sample_user_fields=sample_user_fields,
                           placeholders=placeholders,
                           user_types=user_types,
                           total_users=total_users)

    # Handle message sending (POST request)
    if request.method == 'POST':
        template_id = request.form.get('template_id')
        selected_user_type = request.form.get('user_type')  # This will now work correctly


        selected_template = get_template(template_id)
        
        if not selected_template:
            flash('Invalid template', 'error')
            return redirect(url_for('send_template'))
        
        # Get users based on the selected type
        with sqlite3.connect('database.db') as conn:
            conn.row_factory = sqlite3.Row
            if selected_user_type == 'all':
                users = conn.execute('SELECT * FROM users').fetchall()
            else:
                users = conn.execute('SELECT * FROM users WHERE user_type = ?', 
                                   (selected_user_type,)).fetchall()
        
        # Convert to list of dicts
        users = [dict(user) for user in users]
        
        # Validate we have users to send to
        if not users:
            flash(f'No users found for type: {selected_user_type}', 'warning')
            return redirect(url_for('send_template'))
        
        # Validate all mappings
        placeholder_map = {}
        missing_mappings = []
        placeholders = get_template_variables(selected_template['body'])
        
        for placeholder in placeholders:
            field = request.form.get(f'map_{placeholder}')
            if not field:
                missing_mappings.append(placeholder)
            else:
                placeholder_map[placeholder] = field

        if missing_mappings:
            sample_user_fields = list(users[0].keys()) if users else []
            return render_template('map_fields.html',
                               error=f"Please select fields for: {', '.join(missing_mappings)}",
                               template=selected_template,
                               template_id=template_id,
                               sample_user_fields=sample_user_fields,
                               placeholders=placeholders,
                               user_types=user_types,
                               total_users=total_users)

        # Process and send messages
        sent_results = []
        phone_number_id = session['selected_number']
        
        for user in users:
            try:
                message_body = selected_template['body']
                phone = user.get('phone', '')
                user_name = user.get('name', '')
                
                # Replace placeholders with user data
                for placeholder, field in placeholder_map.items():
                    value = str(user.get(field, f"[MISSING: {field}]"))
                    message_body = message_body.replace(f"{{{{{placeholder}}}}}", value)

                message = clean_param(message_body)

                # Prepare WhatsApp API payload
                payload = {
                    "messaging_product": "whatsapp",
                    "to": phone,
                    "type": "template",
                    "template": {
                        "name": "the_big_beautiful_template",
                        "language": {"code": "en"},
                        "components": [
                            {
                                "type": "body",
                                "parameters": [
                                    {"type": "text", "text": user_name},
                                    {"type": "text", "text": message}
                                ]
                            }
                        ]
                    }
                }

                headers = {
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json"
                }

                # Send message
                url = f"https://graph.facebook.com/v23.0/{phone_number_id}/messages"
                response = requests.post(url, headers=headers, json=payload)
                response_data = response.json() if response.content else None
                
                # Determine message status
                if response.status_code == 200:
                    status = '✅ Sent'
                    message_id = response_data.get('messages', [{}])[0].get('id', 'N/A')
                    delivery_status = 'Delivered to WhatsApp'
                elif response.status_code == 400:
                    status = "❌ Failed (Invalid Request)"
                    error_message = response_data.get('error', {}).get('message', 'Unknown error')
                    delivery_status = f"Failed: {error_message}"
                else:
                    status = f"❌ Failed ({response.status_code})"
                    delivery_status = "Unknown error occurred"

                # Save to DB for webhook tracking
                with sqlite3.connect('database.db') as conn:
                    conn.execute('''
                        INSERT INTO sent_messages 
                        (user, phone, message, message_id, status, delivery_status, timestamp, user_type)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        user.get('name', 'N/A'),
                        phone,
                        message,
                        message_id,
                        status,
                        delivery_status,
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        user.get('user_type', 'unknown')
                    ))

                sent_results.append({
                    'user': user.get('name', 'N/A'),
                    'phone': phone,
                    'status': status,
                    'user_type': user.get('user_type', 'unknown')
                })
                
            except Exception as e:
                sent_results.append({
                    'user': user.get('name', 'N/A'),
                    'phone': user.get('phone', 'N/A'),
                    'status': f"❌ Failed: {str(e)}",
                    'user_type': user.get('user_type', 'unknown')
                })

        # Prepare summary message
        success_count = sum(1 for r in sent_results if r['status'].startswith('✅'))
        failure_count = len(sent_results) - success_count
        flash(
            f"Messages sent successfully to {success_count} users. "
            f"Failed to send to {failure_count} users.", 
            'success' if success_count > 0 else 'warning'
        )
        
        # Store results in session for display
        session['last_send_results'] = {
            'template_name': selected_template['name'],
            'user_type': selected_user_type,
            'results': sent_results,
            'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        
        return redirect(url_for('show_results'))

    # Initial view - show template selection with user type filter
    templates = get_all_templates()
    return render_template('select_template.html', 
                         templates=templates,
                         user_types=user_types,
                         total_users=total_users)






@app.route('/users', methods=['GET', 'POST'])
def manage_users():


    if request.method == 'POST':
        # Handle bulk upload
        if 'excel_file' in request.files:
            file = request.files['excel_file']
            if file.filename.endswith('.xlsx'):
                try:
                    df = pd.read_excel(file)
                    # Validate columns
                    required_cols = ['name', 'phone', 'user_type']
                    if not all(col in df.columns for col in required_cols):
                        flash('Excel file must contain name, phone, and user_type columns', 'error')
                        return redirect(url_for('manage_users'))
                    
                    with sqlite3.connect('database.db') as conn:
                        for _, row in df.iterrows():
                            conn.execute('''
                                INSERT OR REPLACE INTO users 
                                (name, phone, order_id, offer_details, user_type)
                                VALUES (?, ?, ?, ?, ?)
                            ''', (
                                row.get('name'),
                                row.get('phone'),
                                row.get('order_id', ''),
                                row.get('offer_details', ''),
                                row.get('user_type', 'regular')
                            ))
                    flash(f'{len(df)} users imported successfully!', 'success')
                except Exception as e:
                    flash(f'Error processing Excel file: {str(e)}', 'error')
        
        # Handle single user creation
        else:
            name = request.form.get('name')
            phone = request.form.get('phone')
            user_type = request.form.get('user_type', 'regular')
            order_id = request.form.get('order_id', '')
            offer_details = request.form.get('offer_details', '')
            
            if not name or not phone:
                flash('Name and phone are required', 'error')
                return redirect(url_for('manage_users'))
            
            try:
                with sqlite3.connect('database.db') as conn:
                    conn.execute('''
                        INSERT INTO users 
                        (name, phone, order_id, offer_details, user_type)
                        VALUES (?, ?, ?, ?, ?)
                    ''', (name, phone, order_id, offer_details, user_type))
                flash('User added successfully', 'success')
            except sqlite3.IntegrityError:
                flash('User with this phone already exists', 'error')
    
    # Get all users
    with sqlite3.connect('database.db') as conn:
        conn.row_factory = sqlite3.Row
        users = conn.execute('SELECT * FROM users ORDER BY name').fetchall()




        # user_types = conn.execute('SELECT DISTINCT user_type FROM users').fetchall()
        # Get all available user types from database
        with sqlite3.connect('database.db') as conn:
            conn.row_factory = sqlite3.Row
            user_types = conn.execute('''
                SELECT type_name FROM user_types ORDER BY type_name
            ''').fetchall()

        
    
    return render_template('manage_users.html', users=users, user_types=user_types)

@app.route('/users/delete/<int:user_id>', methods=['POST'])
def delete_user(user_id):
    try:
        with sqlite3.connect('database.db') as conn:
            conn.execute('DELETE FROM users WHERE id = ?', (user_id,))
        flash('User deleted successfully', 'success')
    except Exception as e:
        flash(f'Error deleting user: {str(e)}', 'error')
    return redirect(url_for('manage_users'))


@app.route('/download-user-template')
def download_user_template():
    # Create a sample Excel file in memory
    output = BytesIO()
    
    # Create DataFrame with sample data
    data = {
        'name': ['John Doe', 'Jane Smith'],
        'phone': ['+201234567890', '+201098765432'],
        'user_type': ['vip', 'regular'],
        'order_id': ['A1001', 'B2002'],
        'offer_details': ['Special offer', 'Standard offer']
    }
    
    df = pd.DataFrame(data)
    df.to_excel(output, index=False, engine='openpyxl')
    output.seek(0)
    
    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        download_name='User_Import_Template.xlsx',
        as_attachment=True
    )








@app.route('/user_types', methods=['GET', 'POST'])
def manage_user_types():
    if request.method == 'POST':
        type_name = request.form.get('type_name').lower().strip()
        description = request.form.get('description', '').strip()
        
        try:
            with sqlite3.connect('database.db') as conn:
                conn.execute('''
                    INSERT INTO user_types (type_name, description)
                    VALUES (?, ?)
                ''', (type_name, description))
            flash(f'User type "{type_name}" created successfully', 'success')
        except sqlite3.IntegrityError:
            flash(f'User type "{type_name}" already exists', 'error')
        
        return redirect(url_for('manage_user_types'))
    
    with sqlite3.connect('database.db') as conn:
        conn.row_factory = sqlite3.Row
        types = conn.execute('''
            SELECT * FROM user_types ORDER BY type_name
        ''').fetchall()
    
    return render_template('manage_user_types.html', user_types=types)

@app.route('/user_type/delete/<type_name>', methods=['POST'])
def delete_user_type(type_name):
    try:
        with sqlite3.connect('database.db') as conn:
            # Check if any users have this type
            user_count = conn.execute('''
                SELECT COUNT(*) FROM users WHERE user_type = ?
            ''', (type_name,)).fetchone()[0]
            
            if user_count > 0:
                flash(f'Cannot delete - {user_count} users have this type', 'error')
            else:
                conn.execute('''
                    DELETE FROM user_types WHERE type_name = ?
                ''', (type_name,))
                flash(f'User type "{type_name}" deleted', 'success')
    except Exception as e:
        flash(f'Error deleting type: {str(e)}', 'error')
    
    return redirect(url_for('manage_user_types'))








@app.route('/results')
def show_results():
    with sqlite3.connect('database.db') as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute('SELECT * FROM sent_messages ORDER BY timestamp DESC').fetchall()
    return render_template('results.html', messages=rows, template=session.get('last_template'))

@app.route('/webhook', methods=['GET', 'POST'])
def whatsapp_webhook():
    if request.method == 'GET':
        # Verification step for webhook subscription
        VERIFY_TOKEN = "my_verify_token"
        mode = request.args.get('hub.mode')
        token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')

        if mode == "subscribe" and token == VERIFY_TOKEN:
            return challenge, 200
        else:
            return "Verification failed", 403

    elif request.method == 'POST':
        data = request.get_json()
        if not data:
            return "No data", 400

        try:
            changes = data.get("entry", [])[0].get("changes", [])
            for change in changes:
                statuses = change.get("value", {}).get("statuses", [])
                for status_event in statuses:
                    message_id = status_event.get("id")
                    status = status_event.get("status", "").lower()

                    # Map WhatsApp statuses to display values
                    delivery_status_map = {
                        "sent": "📤 Sent (not yet delivered)",
                        "delivered": "✅ Delivered",
                        "read": "👁 Read",
                        "failed": "❌ Failed to deliver"
                    }
                    delivery_status = delivery_status_map.get(status, status)

                    # Update DB with new status
                    with sqlite3.connect('database.db') as conn:
                        conn.execute('''
                            UPDATE sent_messages
                            SET delivery_status = ?, status = ?
                            WHERE message_id = ?
                        ''', (
                            delivery_status,
                            "✅ Sent" if status in ["sent", "delivered", "read"] else "❌ Failed",
                            message_id
                        ))

        except Exception as e:
            print("Webhook processing error:", e)

        return "EVENT_RECEIVED", 200

if __name__ == '__main__':
    init_db()
    app.run(debug=True)