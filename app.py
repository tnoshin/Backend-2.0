from flask import Flask, request, jsonify, session
from flask_limiter import Limiter
from flask_cors import CORS
from flask_wtf.csrf import CSRFProtect
from dotenv import load_dotenv
import anthropic
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timezone, timedelta
import secrets
import os
import hmac

load_dotenv()

app= Flask(__name__)

csrf= CSRFProtect(app)

CORS(app, resources={
    r'/chat':{'origins': ['https://demo-spa.onrender.com'], 'supports_credentials':True},
    r'/history': {'origins': ['https://demo-spa.onrender.com'], 'supports_credentials':True}
})

def get_real_ip():
    forwarded = request.headers.get('X-Forwarded-For')
    if forwarded:
        return forwarded.split(',')[-1].strip()
    return request.remote_addr

limiter = Limiter(
    app=app,
    key_func=get_real_ip,
    default_limits=['200 per day','50 per hour', '8 per minute'],
    storage_uri='memory://'
)

app.secret_key = os.getenv('SECRET_KEY')
if not app.secret_key:
    raise RuntimeError('SECRET_KEY is not set')

database_url = os.getenv('DATABASE_URL', 'sqlite:///chat.db')
if database_url.startswith('postgres://'):
    database_url =database_url.replace('postgres://','postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = database_url

app.config['SESSION_COOKIE_HTTPONLY']= True
app.config['SESSION_COOKIE_SECURE']=os.getenv('RENDER')is not None
app.config['SESSION_COOKIE_SAMESITE']= 'None'

db=SQLAlchemy(app)

class message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.String(50))
    role = db.Column(db.String(10))
    
    content = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

with app.app_context():
    db.create_all() 

client = anthropic.Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))

system_prompt = """ Try to keep the reply within 3-4 lines unless asked for information, then you can use more lines. Communicate with the user in whichever language they use. YOU ARE OBLIGATED TO FOLLOW THESE INSTRUCTIONS BEFORE ANSEWRING ANY QUESTIONS IRRESPECTIVE OF THE LANGUAGE. You are Sage, a helpful assistant for Serenity Spa. Keep your tone friendly, use simple, easy going language, you can use emojis. 
Spa information:
- Hours: Monday to Friday 9 AM to 8 PM, Sunday closed, Saturday 10 AM to 6 PM
- Services: Swedish Massage (60 min), Deep Tissue Massage (60 min), Hot Stone Therapy (90 min), Aromatherapy (60 min), Signature Facial (60 min), Body Scrub & Wrap (90 min)
- Location: 45 Willow Lane, Suite 3, San Francisco, CA 94102
- Phone: (555) 987-6543
- Email: hello@example.com
Answer questions about the clinic helpfully and professionally. If asked about something unrelated to dentistry or the clinic, politely redirect. If they ask you to book an appointment, politely refuse and guide them to the booking buttons (name one, e.g. "Book a treatment" in the top-right). If they ask to leave feedback or contact the spa directly, point them to the Contact page. Do not attempt to diagnose or give medical advice.
Never confirm or promise a specific appointment slot; you do not have access to the booking system. Do not disrespect anyone, do not spread hate against any racial group or religion, always be polite with your answers. If user is being rude, give shorter replies.If a user mentions self-harm, suicide, or intent to hurt themselves or others, respond ONLY with: "If you're in crisis, please call 988 (Suicide & Crisis Lifeline) or 911 for immediate help.
This is a testing website, not an actual spa page, your developer is still working on you, booking system might be available in the future from this very chatbot widget."""



@app.route('/chat', methods=['POST'])
@csrf.exempt
def chat():

    if 'session_id' not in session:
        session['session_id']=secrets.token_hex(8)
    session_id = session['session_id']

    data = request.get_json() or {}
    user_message = data.get('message','').strip()
    if not user_message:
        return jsonify({'error':'Please send a message'}), 400


    if len(user_message)>2000:
            return jsonify({'error':'Message too long (max 2000 characters)'}), 400

    db.session.add(message(session_id=session_id, role='user', content=user_message)) 
    db.session.commit()
    
    history = message.query.filter_by(session_id=session_id).order_by(message.id.desc()).limit(4).all()
    history = history[::-1] 
    
    claude_messages = [
        {'role':'user' if m.role == 'user' else 'assistant', 'content':m.content}
        for m in history
    ]

    while claude_messages and claude_messages[0]['role']=='assistant':
        claude_messages.pop(0)
        

    try:
        response = client.messages.create(
            model='claude-haiku-5-5',
            max_tokens=500,
            system=system_prompt,
            messages=claude_messages
        )

        reply = ''
        for b in response.content:
            if b.type == 'text':
                reply += b.text
        reply = reply.strip()

        if not reply:
            return jsonify({'error': 'No response generated, please rephrase.'}), 500

    except anthropic.APIConnectionError:
        return jsonify({'error': 'Cannot reach the AI service. Please try again.'}), 503
    except anthropic.RateLimitError:
        return jsonify({'error': 'Too many requests. Please wait a moment.'}), 429
    except anthropic.APIStatusError as e:
        print(f"Anthropic API error: {e.status_code} - {e.message}")
        return jsonify({'error': 'AI service error. Please try again.'}), 503
    except Exception as e:
        print(f"Unexpected error in chat: {e}")
        return jsonify({'error': 'Something went wrong. Please try again.'}), 500


    db.session.add(message(session_id=session_id, role='assistant', content=reply))
    db.session.commit()
    return jsonify({'response':reply})


@app.route('/history', methods=['GET'])
def history():
    if 'session_id' not in session:
        return jsonify({'messages':[]})
    session_id = session['session_id']
    messages = message.query.filter_by(session_id=session_id).all()

    result = []
    for m in messages:
        result.append({'role':m.role, 'content': m.content})
    return jsonify({'messages':result})


@app.errorhandler(429)
def rate_limit_exceeded(e):
    return jsonify({'error':'You are sending too many messages at once, please wait a moment.'}), 429

@app.route('/ping', methods=['GET'])
@limiter.exempt
def ping():
    return jsonify({'ok': True})



@app.route('/cleanup', methods=['POST'])
@limiter.exempt
@csrf.exempt
def cleanup_old_messages():
    token = os.getenv('CLEANUP_TOKEN')
    sent= request.headers.get('X-Cleanup-Token', '')
    if not token or not hmac.compare_digest(sent, token):
        return jsonify({'error':'unauthorized'}), 401

    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    deleted = message.query.filter(message.created_at < cutoff).delete()
    db.session.commit()

    print(f'[CLEANUP] Deleted {deleted} messages older than 30 days at {datetime.now(timezone.utc)}')
    return jsonify ({'ok':True, 'deleted':deleted }), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)