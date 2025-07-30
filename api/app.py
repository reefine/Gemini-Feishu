# api/app.py (增强版)

import os
import json
import requests
from flask import Flask, request, jsonify
import google.generativeai as genai
from vercel_kv import KV

# --- 配置区: 从 Vercel 的环境变量中读取密钥 ---
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# 初始化服务
app = Flask(__name__)
try:
    genai.configure(api_key=GEMINI_API_KEY)
    gemini_model = genai.GenerativeModel('gemini-1.5-flash-latest')
    kv = KV()
    print("Gemini and Vercel KV initialized successfully.")
except Exception as e:
    print(f"Error initializing services: {e}")

# ... (get_feishu_tenant_token 和 reply_to_feishu 函数保持不变)
def get_feishu_tenant_token():
    token = kv.get('FEISHU_TENANT_ACCESS_TOKEN')
    if token: return token
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    payload = {"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}
    try:
        response = requests.post(url, headers={"Content-Type": "application/json"}, json=payload, timeout=5)
        data = response.json()
        if data.get("code") == 0:
            token = data.get("tenant_access_token")
            kv.set('FEISHU_TENANT_ACCESS_TOKEN', token, ex=6600)
            return token
    except Exception as e:
        print(f"Error getting token: {e}")
    return None

def reply_to_feishu(message_id, content):
    token = get_feishu_tenant_token()
    if not token: return
    url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
    payload = {"msg_type": "text", "content": json.dumps({"text": content})}
    try:
        requests.post(url, headers=headers, json=payload, timeout=5)
    except Exception as e:
        print(f"Error replying to feishu: {e}")


def get_conversation_history(session_id):
    history = kv.get(session_id)
    return history if history else []

def save_conversation_history(session_id, history):
    kv.set(session_id, history, ex=3600)

def clear_conversation_history(session_id):
    kv.delete(session_id)


@app.route('/', defaults={'path': ''}, methods=['GET', 'POST'])
@app.route('/<path:path>', methods=['GET', 'POST'])
def webhook_handler(path):
    data = request.json

    # --- 逻辑分流：判断是飞书聊天事件还是多维表格的直接调用 ---

    # 1. 如果是飞书聊天事件 (包含 header 和 event 结构)
    if data and data.get("header", {}).get("event_type") == "im.message.receive_v1":
        # (这部分是原来的聊天机器人逻辑，保持不变)
        if "challenge" in data:
            return jsonify({"challenge": data["challenge"]})
        
        event = data.get("event", {})
        message = event.get("message", {})
        sender = event.get("sender", {})
        message_id = message.get("message_id")
        session_id = f"feishu_session_{message.get('chat_id')}_{sender.get('sender_id', {}).get('user_id')}"
        
        try:
            content_json = json.loads(message.get("content", "{}"))
            user_text = content_json.get("text", "").strip().replace("@_user_1", "").strip()
        except (json.JSONDecodeError, AttributeError):
            user_text = ""

        if not user_text: return jsonify({"status": "empty message ignored"})

        if user_text.lower() == "/clear":
            clear_conversation_history(session_id)
            reply_to_feishu(message_id, "✅ 历史对话已清除。")
            return jsonify({"status": "command processed"})

        try:
            history = get_conversation_history(session_id)
            chat = gemini_model.start_chat(history=history)
            response = chat.send_message(user_text)
            save_conversation_history(session_id, chat.history)
            reply_to_feishu(message_id, response.text)
        except Exception as e:
            reply_to_feishu(message_id, f"机器人出错了: {e}")
        
        return jsonify({"status": "chat event processed"})

    # 2. 如果是来自多维表格的请求 (结构更简单，我们自己定义)
    # 我们约定，多维表格会发送一个包含 "input_text" 字段的JSON
    elif data and "input_text" in data:
        print("Received a request from Bitable.")
        input_text = data.get("input_text")
        
        if not input_text:
            return jsonify({"error": "input_text is empty"}), 400
        
        try:
            # 直接调用Gemini，不处理上下文历史
            response = gemini_model.generate_content(input_text)
            # 将结果直接返回给多维表格
            return jsonify({"result": response.text})
        except Exception as e:
            print(f"Error processing Bitable request: {e}")
            return jsonify({"error": str(e)}), 500
            
    # 如果是其他未知请求（比如浏览器直接访问）
    return "Unsupported Media Type: This endpoint is for Feishu webhooks.", 415


if __name__ == "__main__":
    app.run(debug=True)
