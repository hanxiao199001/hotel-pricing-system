#!/usr/bin/env python3
"""
酒店智能调价系统 - 主程序
"""
import sys
import time
from datetime import datetime
from typing import Dict

# 导入模块
import config as cfg
from database import PricingDatabase
from algorithm import PricingAlgorithm
from ctrip_crawler import create_crawler

def notifier.send_pricing_notification(message: str):
    """发送微信通知"""
    if not cfg.WECHAT_CONFIG['enabled']:
        return
    
    print(f"\n📱 微信通知:")
    print("-" * 50)
    print(message)
    print("-" * 50)
    
    # TODO: 使用 Clawdbot message 工具发送实际消息
    # 临时通过文件记录，后续可通过 HTTP 调用 Clawdbot API
    try:
        with open('/tmp/hotel_pricing_notification.txt', 'a') as f:
            f.write(f"\n[{datetime.now()}]\n{message}\n")
    except:
        pass

def run_pricing_check():
    """执行一次定价检查"""
    print("\n" + "="*60)
    print(f"🏨 开始定价检查 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*60)
    
    # 1. 初始化
    db = PricingDatabase(cfg.DATABASE['path'])
    crawler = create_crawler(cfg.CTRIP_CONFIG)
    algorithm = PricingAlgorithm(cfg.PRICING_STRATEGY)
    
    # 2. 登录携程
    if not crawler.login():
        print("❌ 携程登录失败")
        return
    
    # 3. 获取自己酒店数据
    my_data = crawler.get_my_hotel_data()
    if not my_data:
        print("❌ 获取酒店数据失败")
        return
    
    current_price = my_data['current_price']
    occupancy_rate = my_data['occupancy_rate']
    
    # 保存自己的数据
    db.save_my_price(
        date=my_data['date'],
        price=current_price,
        occupancy_rate=occupancy_rate,
        rooms_available=my_data['rooms_available'],
        rooms_total=my_data['rooms_total']
    )
    
    # 4. 获取竞品价格
    competitors = crawler.get_competitor_hotels(
        city=cfg.COMPETITOR_FILTER['city'],
        distance_km=cfg.COMPETITOR_FILTER['distance_km'],
        price_min=cfg.COMPETITOR_FILTER['price_min'],
        price_max=cfg.COMPETITOR_FILTER['price_max']
    )
    
    if not competitors:
        print("❌ 未找到竞品酒店")
        return
    
    # 保存竞品数据
    db.save_competitor_prices(date=my_data['date'], competitors=competitors)
    
    competitor_prices = [c['price'] for c in competitors]
    comp_min = min(competitor_prices)
    comp_avg = sum(competitor_prices) / len(competitor_prices)
    comp_max = max(competitor_prices)
    
    # 5. 计算最优价格
    suggested_price, reason = algorithm.calculate_optimal_price(
        occupancy_rate=occupancy_rate,
        current_price=current_price,
        competitor_prices=competitor_prices
    )
    
    # 6. 分析市场定位
    position = algorithm.analyze_market_position(
        my_price=current_price,
        competitor_prices=competitor_prices
    )
    
    # 7. 保存建议
    db.save_price_suggestion(
        date=my_data['date'],
        current_price=current_price,
        suggested_price=suggested_price,
        reason=reason,
        occupancy_rate=occupancy_rate,
        competitor_stats={
            'min': comp_min,
            'avg': comp_avg,
            'max': comp_max
        }
    )
    
    # 8. 生成报告
    print("\n" + "="*60)
    print("📊 定价分析报告")
    print("="*60)
    
    print(f"\n【当前状态】")
    print(f"  当前价格: ¥{current_price}")
    print(f"  入住率: {occupancy_rate:.1f}%")
    print(f"  可用房间: {my_data['rooms_available']}/{my_data['rooms_total']}")
    
    print(f"\n【竞品分析】")
    print(f"  竞品数量: {len(competitors)}家")
    print(f"  最低价: ¥{comp_min:.2f}")
    print(f"  平均价: ¥{comp_avg:.2f}")
    print(f"  最高价: ¥{comp_max:.2f}")
    
    print(f"\n【市场定位】")
    print(f"  价格排名: 第{position['rank']}位 / 共{position['total']}家")
    print(f"  定位: {position['position']}")
    
    print(f"\n【调价建议】")
    price_change = suggested_price - current_price
    change_symbol = "+" if price_change > 0 else ""
    print(f"  建议价格: ¥{suggested_price} ({change_symbol}¥{price_change:.2f})")
    print(f"  调价理由: {reason}")
    
    # 9. 发送通知
    if abs(price_change) >= 5:
        notification = f"""🏨 酒店调价建议

📊 当前状态
• 价格: ¥{current_price}
• 入住率: {occupancy_rate:.1f}%
• 排名: 第{position['rank']}位/{position['total']}家

🔍 竞品分析
• 竞品均价: ¥{comp_avg:.2f}
• 价格区间: ¥{comp_min:.2f} - ¥{comp_max:.2f}

💡 调价建议
• 建议价格: ¥{suggested_price}
• 调整幅度: {change_symbol}¥{price_change:.2f}
• 调价理由: {reason}

⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"""
        
        notifier.send_pricing_notification(notification)
    else:
        print("\n✅ 价格合理，无需调整")
    
    print("\n" + "="*60)
    print("✅ 检查完成")
    print("="*60 + "\n")

def main():
    """主函数"""
    print("🏨 酒店智能调价系统")
    print("版本: MVP v1.0")
    print("模式: 建议模式（需手动确认调价）\n")
    
    # 执行一次检查
    try:
        run_pricing_check()
    except Exception as e:
        print(f"❌ 错误: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()

# 导入通知模块
from notifier import WhatsAppNotifier
