#!/usr/bin/env python3
"""
WhatsApp 发送脚本
读取触发文件，通过本地 Clawdbot 实例发送消息
"""
import json
import os
import sys
import time

TRIGGER_FILE = "/tmp/hotel_pricing_notification_trigger.json"

def send_via_local_clawdbot(recipient, message):
    """通过本地 Clawdbot Python API 发送"""
    # 导入 Clawdbot tools（如果可用）
    # 或者通过文件系统与 Clawdbot 通信
    
    # 简化方案：写入 Clawdbot 可读取的位置
    notification_dir = "/root/.clawdbot/hotel_notifications"
    os.makedirs(notification_dir, exist_ok=True)
    
    notify_file = os.path.join(notification_dir, f"notify_{int(time.time())}.json")
    with open(notify_file, "w") as f:
        json.dump({
            "recipient": recipient,
            "message": message,
            "timestamp": time.time()
        }, f)
    
    print(f"✅ 通知已写入: {notify_file}")
    return True

def main():
    if not os.path.exists(TRIGGER_FILE):
        print("无待发送通知")
        return
    
    try:
        with open(TRIGGER_FILE, "r") as f:
            data = json.load(f)
        
        recipient = data["recipient"]
        message = data["message"]
        
        print(f"发送通知到: {recipient}")
        success = send_via_local_clawdbot(recipient, message)
        
        if success:
            os.remove(TRIGGER_FILE)
            print("✅ 通知发送完成")
    
    except Exception as e:
        print(f"❌ 错误: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
