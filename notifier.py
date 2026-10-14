#!/usr/bin/env python3
"""
酒店调价系统 - WhatsApp 通知模块
通过 Clawdbot message 工具发送通知
"""
import os
import subprocess
import json
from datetime import datetime

class WhatsAppNotifier:
    def __init__(self, recipient: str = None):
        # 接收人不再硬编码，默认从环境变量 NOTIFY_RECIPIENT 读取
        self.recipient = recipient or os.environ.get("NOTIFY_RECIPIENT", "")
    
    def send_pricing_notification(self, data: dict):
        """发送调价建议通知"""
        
        # 构建消息
        message = self._format_message(data)
        
        # 通过 Clawdbot message 工具发送
        success = self._send_via_clawdbot(message)
        
        if success:
            print(f"✅ WhatsApp 通知已发送")
        else:
            print(f"❌ WhatsApp 通知发送失败")
            # 降级：写入文件
            self._fallback_to_file(message)
        
        return success
    
    def _format_message(self, data: dict) -> str:
        """格式化消息内容"""
        current_price = data.get('current_price', 0)
        suggested_price = data.get('suggested_price', 0)
        occupancy_rate = data.get('occupancy_rate', 0)
        rank = data.get('rank', 0)
        total = data.get('total', 0)
        comp_avg = data.get('comp_avg', 0)
        comp_min = data.get('comp_min', 0)
        comp_max = data.get('comp_max', 0)
        reason = data.get('reason', '')
        
        price_change = suggested_price - current_price
        change_symbol = "+" if price_change > 0 else ""
        
        message = f"""🏨【酒店调价系统】

📊 海口格林豪泰酒店

当前状态：
• 价格 ¥{current_price:.0f}
• 入住率 {occupancy_rate:.1f}%
• 排名 第{rank}位/{total}家

竞品分析：
• 均价 ¥{comp_avg:.0f}
• 区间 ¥{comp_min:.0f} - ¥{comp_max:.0f}

💡 调价建议：
• 建议价格 ¥{suggested_price:.0f} ({change_symbol}¥{abs(price_change):.0f})
• 理由：{reason}

⏰ {datetime.now().strftime('%Y-%m-%d %H:%M')}"""
        
        return message
    
    def _send_via_clawdbot(self, message: str) -> bool:
        """通过 Clawdbot 发送消息"""
        try:
            # 使用 clawdbot message 命令
            # 注意：需要确保 Clawdbot Gateway 正在运行
            
            # 方案1: 通过命令行（如果 clawdbot 命令可用）
            # result = subprocess.run(
            #     ['clawdbot', 'message', 'send', '--to', self.recipient, '--text', message],
            #     capture_output=True,
            #     text=True,
            #     timeout=30
            # )
            # return result.returncode == 0
            
            # 方案2: 写入触发文件，由外部脚本读取并发送
            trigger_file = '/tmp/hotel_pricing_notification_trigger.json'
            with open(trigger_file, 'w') as f:
                json.dump({
                    'recipient': self.recipient,
                    'message': message,
                    'timestamp': datetime.now().isoformat()
                }, f)
            
            print(f"📝 通知已写入触发文件: {trigger_file}")
            return True
            
        except Exception as e:
            print(f"❌ Clawdbot 发送失败: {e}")
            return False
    
    def _fallback_to_file(self, message: str):
        """降级方案：写入文件"""
        try:
            with open('/tmp/hotel_pricing_notification.txt', 'a') as f:
                f.write(f"\n{'='*60}\n")
                f.write(f"[{datetime.now()}]\n")
                f.write(message)
                f.write(f"\n{'='*60}\n")
            print("📝 通知已写入备份文件")
        except Exception as e:
            print(f"❌ 写入文件失败: {e}")

def send_notification(
    current_price: float,
    suggested_price: float,
    occupancy_rate: float,
    rank: int,
    total: int,
    comp_avg: float,
    comp_min: float,
    comp_max: float,
    reason: str
) -> bool:
    """快捷发送通知"""
    notifier = WhatsAppNotifier()
    return notifier.send_pricing_notification({
        'current_price': current_price,
        'suggested_price': suggested_price,
        'occupancy_rate': occupancy_rate,
        'rank': rank,
        'total': total,
        'comp_avg': comp_avg,
        'comp_min': comp_min,
        'comp_max': comp_max,
        'reason': reason
    })
