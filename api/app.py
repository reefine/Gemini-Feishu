# api/app.py

import os
import json
import time
import requests
from flask import Flask, request, jsonify
import google.generativeai as genai
from vercel_kv import KV

# --- 配置区: 从 Vercel 的环境变量中读取密钥 ---
# 请确保在 Vercel 项目设置中配置了这些环境变量
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# 初始化 Flask 应用
app = Flask(__name__)

# 配置并初始化 Gemini 和 Vercel KV (用于存储对话历史)
try:
    genai.configure(api_key=GEMINI_API_KEY)
    gemini_model = genai.GenerativeModel('gemini-1.5-flash-latest') # 使用最新的 Flash 模型
    kv = KV()
    print("Gemini and Vercel KV initialized successfully.")
except Exception as e:
    print(f"Error initializing services: {e}")

def get_feishu_tenant_token():
    """获取并缓存飞书的 tenant_access_token"""
    token = kv.get('FEISHU_TENANT_ACCESS_TOKEN')
    if token:
        return token

    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    headers = {"Content-Type": "application/json"}
    payload = {"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}
    
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=5)
        response.raise_for_status()
        data = response.json()
        if data.get("code") == 0:
            token = data.get("tenant_access_token")
            # 缓存 token，并设置比官方有效期稍短的过期时间（例如110分钟）
            kv.set('FEISHU_TENANT_ACCESS_TOKEN', token, ex=6600)
            print("Successfully fetched and cached tenant_access_token.")
            return token
        else:
            print(f"Failed to get token, error from Feishu: {data.get('msg')}")
            return None
    except requests.RequestException as e:
        print(f"Request exception while getting token: {e}")
        return None

def reply_to_feishu(message_id, content):
    """调用飞书API回复消息"""
    token = get_feishu_tenant_token()
    if not token:
        print("Cannot reply: missing tenant_access_token.")
        return

    url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    payload = {
        "msg_type": "text",
        "content": json.dumps({"text": content})
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=5)
        response.raise_for_status()
        print(f"Successfully replied to message_id {message_id}.")
    except requests.RequestException as e:
        print(f"Failed to reply to Feishu: {e}")

def get_conversation_history(session_id):
    """从 Vercel KV 中获取历史对话"""
    history = kv.get(session_id)
    return history if history else []

def save_conversation_history(session_id, history):
    """将更新后的对话历史存入 Vercel KV，并设置1小时过期"""
    kv.set(session_id, history, ex=3600) # ex=3600秒，即1小时

def clear_conversation_history(session_id):
    """清除历史对话"""
    kv.delete(session_id)

@app.route('/', defaults={'path': ''}, methods=['GET', 'POST'])
@app.route('/<path:path>', methods=['GET', 'POST'])
def webhook_handler(path):
    data = request.json
    
    # 1. 处理飞书的 URL 验证挑战
    if data and "challenge" in data:
        return jsonify({"challenge": data["challenge"]})

    # 2. 检查事件类型是否为消息接收
    header = data.get("header", {})
    if header.get("event_type") != "im.message.receive_v1":
        return jsonify({"status": "event ignored"})

    # 3. 解析关键信息
    event = data.get("event", {})
    message = event.get("message", {})
    sender = event.get("sender", {})
    
    message_id = message.get("message_id")
    chat_type = message.get("chat_type")
    
    # 创建唯一的会话ID
    session_id = f"feishu_session_{message.get('chat_id')}_{sender.get('sender_id', {}).get('user_id')}"
    
    # 提取用户文本，并去除@机器人的部分
    try:
        content_json = json.loads(message.get("content", "{}"))
        user_text = content_json.get("text", "").strip().replace("@_user_1", "").strip()
    except (json.JSONDecodeError, AttributeError):
        user_text = ""

    if not user_text:
        return jsonify({"status": "empty message ignored"})

    # 4. 处理指令
    if user_text.lower() == "/clear":
        clear_conversation_history(session_id)
        reply_to_feishu(message_id, "✅ 历史对话已清除，我们可以开始新的话题了。")
        return jsonify({"status": "success"})

    # 5. 调用 Gemini 并回复
    try:
        # 获取历史对话
        history = get_conversation_history(session_id)
        
        # 启动一个带历史记录的聊天
        chat = gemini_model.start_chat(history=history)
        
        # 发送新消息
        response = chat.send_message(user_text)
        gemini_reply = response.text
        
        # 保存新的对话记录
        save_conversation_history(session_id, chat.history)

        # 回复给飞书用户
        reply_to_feishu(message_id, gemini_reply)
    
    except Exception as e:
        print(f"Error during Gemini processing: {e}")
        reply_to_feishu(message_id, f"机器人出了一点小问题，请稍后再试。\n错误: {e}")

    return jsonify({"status": "success"})

# Flask app 的入口
if __name__ == "__main__":
    app.run(debug=True)
