"""
定价算法模块 - 智能计算最优价格
"""
from typing import List, Dict, Tuple
import config

class PricingAlgorithm:
    def __init__(self, config_dict: Dict):
        self.config = config_dict
    
    def calculate_optimal_price(
        self,
        occupancy_rate: float,
        current_price: float,
        competitor_prices: List[float]
    ) -> Tuple[float, str]:
        """
        计算最优价格
        
        Args:
            occupancy_rate: 当前入住率 (0-100)
            current_price: 当前价格
            competitor_prices: 竞品价格列表
        
        Returns:
            (建议价格, 调价理由)
        """
        if not competitor_prices:
            return current_price, "无竞品数据"
        
        # 竞品价格统计
        comp_min = min(competitor_prices)
        comp_avg = sum(competitor_prices) / len(competitor_prices)
        comp_max = max(competitor_prices)
        
        # 基于入住率的策略
        if occupancy_rate < self.config['low_occupancy_threshold']:
            # 入住率低 → 降价吸客
            suggested_price = comp_avg - self.config['low_occupancy_discount']
            reason = f"入住率低({occupancy_rate:.1f}%)，建议降价吸引客流"
        
        elif occupancy_rate > self.config['high_occupancy_threshold']:
            # 入住率高 → 涨价提升收益
            suggested_price = comp_avg + self.config['high_occupancy_premium']
            reason = f"入住率高({occupancy_rate:.1f}%)，建议涨价提升收益"
        
        else:
            # 入住率中等 → 跟随竞品
            suggested_price = comp_avg - self.config['follow_competitor_gap']
            reason = f"入住率正常({occupancy_rate:.1f}%)，略低于竞品平均价保持竞争力"
        
        # 价格保护
        suggested_price = max(
            self.config['min_price'],
            min(suggested_price, self.config['max_price'])
        )
        
        # 价格微调（避免频繁小幅调整）
        price_diff = abs(suggested_price - current_price)
        if price_diff < 5:
            return current_price, "价格变化不大，维持当前价格"
        
        return round(suggested_price, 0), reason
    
    def analyze_market_position(
        self,
        my_price: float,
        competitor_prices: List[float]
    ) -> Dict:
        """分析市场定位"""
        if not competitor_prices:
            return {"position": "未知", "rank": 0, "total": 0}
        
        all_prices = sorted(competitor_prices + [my_price])
        my_rank = all_prices.index(my_price) + 1
        total = len(all_prices)
        
        if my_rank == 1:
            position = "最低价"
        elif my_rank <= total * 0.3:
            position = "低价位"
        elif my_rank <= total * 0.7:
            position = "中价位"
        else:
            position = "高价位"
        
        return {
            "position": position,
            "rank": my_rank,
            "total": total,
            "percentile": (my_rank / total) * 100
        }
