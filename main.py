#!/usr/bin/env python3
"""酒店智能调价系统 - 命令行入口

只做两件事：调一次询价工作流(pipeline)、把结果打印成报告并发通知。
数据的采集、校验、算价、落库全在 pipeline.run_pricing_check 里，
中台（webapp）调用的是同一份实现，两边口径不会分叉。
"""
import sys
from datetime import datetime

from app import config as cfg
from app.database import DatabasePathError
from app.pricing import pipeline
from app.pricing.pipeline import WorkflowError
from scrapers.runner import ScraperError
from scrapers.validator import DataValidationError


def send_wechat_notification(message: str):
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
    except Exception:
        pass


def print_report(result: dict):
    """把询价结果打印成可读报告。"""
    my = result["my"]
    stats = result["stats"]
    detail = result["suggestion"]
    position = result["position"]
    current_price = my["current_price"]
    suggested_price = detail["suggested_price"]

    print("\n" + "=" * 60)
    print("📊 定价分析报告")
    print("=" * 60)

    print("\n【当前状态】")
    print(f"  当前价格: ¥{current_price}")
    print(f"  入住率: {my['occupancy_rate']:.1f}%")
    if my.get('rooms_total'):
        print(f"  可用房间: {my.get('rooms_available')}/{my['rooms_total']}")

    print("\n【竞品分析】")
    print(f"  竞品数量: {stats['count']}家")
    print(f"  最低价: ¥{stats['min']:.2f}")
    print(f"  中位价: ¥{stats['median']:.2f}   ← 算法依据")
    print(f"  最高价: ¥{stats['max']:.2f}")
    print(f"  平均价: ¥{stats['avg']:.2f}   （仅供参考，易被少数高价酒店拉偏）")

    print("\n【市场定位】")
    print(f"  价格排名: 第{position['rank']}位 / 共{position['total']}家")
    print(f"  定位: {position['position']}")

    print("\n【调价建议】")
    price_change = suggested_price - current_price
    change_symbol = "+" if price_change > 0 else ""
    change_pct = price_change / current_price * 100 if current_price else 0
    print(f"  建议价格: ¥{suggested_price} "
          f"({change_symbol}¥{price_change:.2f}, {change_pct:+.1f}%)")
    print(f"  调价理由: {detail['reason']}")

    return price_change


def run_pricing_check():
    """执行一次定价检查（工作流 + 报告 + 通知）。"""
    print("\n" + "=" * 60)
    print(f"🏨 开始定价检查 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    result = pipeline.run_pricing_check()
    price_change = print_report(result)

    if abs(price_change) >= 5:
        my = result["my"]
        stats = result["stats"]
        detail = result["suggestion"]
        position = result["position"]
        change_symbol = "+" if price_change > 0 else ""

        notification = f"""🏨 酒店调价建议

📊 当前状态
• 价格: ¥{my['current_price']}
• 入住率: {my['occupancy_rate']:.1f}%
• 排名: 第{position['rank']}位/{position['total']}家

🔍 竞品分析
• 竞品均价: ¥{stats['avg']:.2f}
• 价格区间: ¥{stats['min']:.2f} - ¥{stats['max']:.2f}

💡 调价建议
• 建议价格: ¥{detail['suggested_price']}
• 调整幅度: {change_symbol}¥{price_change:.2f}
• 调价理由: {detail['reason']}

⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"""

        send_wechat_notification(notification)
    else:
        print("\n✅ 价格合理，无需调整")

    print("\n" + "=" * 60)
    print("✅ 检查完成")
    print("=" * 60 + "\n")


def _exit_on_config_error(exc: cfg.ConfigError):
    print(f"❌ 启动失败: {exc}", file=sys.stderr)
    raise SystemExit(1) from exc


def main():
    """主函数"""
    print("🏨 酒店智能调价系统")
    print("版本: MVP v1.1")
    print("模式: 建议模式（需手动确认调价）\n")

    # 必填环境变量缺失时立刻退出，不要进入爬虫
    try:
        cfg.validate_startup_config()
    except cfg.ConfigError as exc:
        _exit_on_config_error(exc)

    try:
        run_pricing_check()
    except cfg.ConfigError as exc:
        _exit_on_config_error(exc)
    except DatabasePathError as exc:
        print(f"❌ 启动失败: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except (ScraperError, DataValidationError, WorkflowError) as exc:
        print(f"❌ 采集失败: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except Exception as e:
        print(f"❌ 错误: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
