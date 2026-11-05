"""
定价算法模块 - 基于「同档竞品中位价」计算建议价格

三条设计原则（都是真实数据逼出来的）：

1. 用中位数而非算术均值
   算术均值会被少数高价酒店严重拉偏——实测 48 家里一家 ¥697 的五星酒店
   就把均值从 ¥220 抬到 ¥242。中位数对极端值不敏感，更能代表同档水平。

2. 用比例而非固定金额
   原来的 -10/-5/+10 元，对 ¥100 的快捷酒店是 10%，对 ¥600 的酒店只有 1.6%，
   力度完全不对等。改成相对中位价的百分比，各价位一致。

3. 只跟「同档」竞品比
   把 ¥63 的客栈和 ¥697 的五星放在一起平均没有业务含义。先按当前价
   上下一定比例切出同档竞品，样本不足时再退回全量。

另外单次调价幅度设上限：目标价可能离当前价很远（当前价明显偏离市场时），
一步到位容易踩空，分多步靠近更稳。
"""
import statistics
from typing import Dict, List, Tuple


class PricingAlgorithm:
    def __init__(self, config_dict: Dict):
        self.config = config_dict

    def _pick_peers(self, current_price: float, competitor_prices: List[float]):
        """挑出与当前价同档的竞品。

        返回 (peers, fell_back)，fell_back=True 表示同档样本不足、退回了全量。
        """
        low = current_price * self.config["band_low_ratio"]
        high = current_price * self.config["band_high_ratio"]
        band = [p for p in competitor_prices if low <= p <= high]
        if len(band) >= self.config["band_min_samples"]:
            return band, False
        return list(competitor_prices), True

    def calculate_optimal_price(
        self,
        occupancy_rate: float,
        current_price: float,
        competitor_prices: List[float]
    ) -> Tuple[float, str]:
        """计算最优价格，返回 (建议价格, 调价理由)。

        保留这个签名是为了不打断既有调用方（main.py / 测试）。
        需要中间量（同档中位价、理论目标价、是否触发单次上限）时用 explain()。
        """
        detail = self.explain(occupancy_rate, current_price, competitor_prices)
        return detail["suggested_price"], detail["reason"]

    def explain(
        self,
        occupancy_rate: float,
        current_price: float,
        competitor_prices: List[float]
    ) -> Dict:
        """算出建议价，并把每一步的中间量一并返回。

        中台要展示「为什么是这个价」，而不是只给一个数字。原来这些中间量
        只存在于函数局部变量里，只能靠解析 reason 字符串反推，很脆弱。
        """
        if not competitor_prices:
            return {
                "suggested_price": current_price,
                "ideal_price": current_price,
                "reason": "无竞品数据",
                "occupancy_rate": occupancy_rate,
                "current_price": current_price,
                "peers_count": 0,
                "peers_median": None,
                "fell_back": False,
                "ratio": 0.0,
                "stepped": False,
                "no_change": True,
                "band": None,
            }

        peers, fell_back = self._pick_peers(current_price, competitor_prices)
        base = statistics.median(peers)

        # 入住率决定相对中位价的偏移方向
        if occupancy_rate < self.config["low_occupancy_threshold"]:
            ratio = self.config["low_occupancy_ratio"]
            headline = (
                f"入住率偏低({occupancy_rate:.1f}%)，低于同档中位价让价吸客"
            )
        elif occupancy_rate > self.config["high_occupancy_threshold"]:
            ratio = self.config["high_occupancy_ratio"]
            headline = (
                f"入住率偏高({occupancy_rate:.1f}%)，高于同档中位价提升收益"
            )
        else:
            ratio = self.config["normal_occupancy_ratio"]
            headline = (
                f"入住率正常({occupancy_rate:.1f}%)，贴近同档中位价保持竞争力"
            )

        target = base * (1 + ratio)

        # 相对边界：不允许偏离同档中位价太远
        target = min(
            max(target, base * self.config["floor_ratio"]),
            base * self.config["cap_ratio"],
        )

        # 绝对成本底线（max_price 为 None 表示不设绝对上限）
        target = max(target, self.config["min_price"])
        cap_abs = self.config.get("max_price")
        if cap_abs is not None:
            target = min(target, cap_abs)

        # 此时 target 是「理论上应该定的价」，先记下来用于解释
        ideal = target

        # 单次调价幅度上限：目标再远也分步走
        step = current_price * self.config["max_step_ratio"]
        stepped = False
        if abs(target - current_price) > step:
            target = current_price + (step if target > current_price else -step)
            stepped = True

        # 幅度收敛后再兜一次成本底线，避免封顶后跌破
        target = max(target, self.config["min_price"])

        band_low = current_price * self.config["band_low_ratio"]
        band_high = current_price * self.config["band_high_ratio"]

        if abs(target - current_price) < self.config["min_change"]:
            return {
                "suggested_price": current_price,
                "ideal_price": ideal,
                "reason": "价格变化不大，维持当前价格",
                "occupancy_rate": occupancy_rate,
                "current_price": current_price,
                "peers_count": len(peers),
                "peers_median": base,
                "fell_back": fell_back,
                "ratio": ratio,
                "stepped": False,
                "no_change": True,
                "band": {"low": band_low, "high": band_high},
            }

        detail = f"同档{len(peers)}家中位¥{base:.0f}"
        if fell_back:
            detail += "（同档样本不足，参考全部竞品）"
        if stepped:
            gap_pct = (ideal - current_price) / current_price * 100
            detail += (
                f"；理论目标¥{ideal:.0f}，与当前价相差{gap_pct:+.0f}%，"
                f"超出单次调整上限{self.config['max_step_ratio'] * 100:.0f}%，建议分步靠近"
            )

        return {
            "suggested_price": round(target, 0),
            "ideal_price": round(ideal, 0),
            "reason": f"{headline}；{detail}",
            "occupancy_rate": occupancy_rate,
            "current_price": current_price,
            "peers_count": len(peers),
            "peers_median": base,
            "fell_back": fell_back,
            "ratio": ratio,
            "stepped": stepped,
            "no_change": False,
            "band": {"low": band_low, "high": band_high},
        }

    def analyze_market_position(
        self,
        my_price: float,
        competitor_prices: List[float]
    ) -> Dict:
        """分析市场定位

        名次用「严格比我便宜的家数 + 1」计算，价格并列时视为同名次；
        原实现用 list.index()，并列时会把自己排到最前面，名次偏低。
        """
        if not competitor_prices:
            return {"position": "未知", "rank": 0, "total": 0}

        total = len(competitor_prices) + 1
        my_rank = sum(1 for p in competitor_prices if p < my_price) + 1
        percentile = my_rank / total

        if my_rank == 1:
            position = "最低价"
        elif percentile <= 0.25:
            position = "低价位"
        elif percentile <= 0.75:
            position = "中价位"
        else:
            position = "高价位"

        return {
            "position": position,
            "rank": my_rank,
            "total": total,
            "percentile": percentile * 100,
        }
