# T+7复盘方法

## 数据门槛

观察不少于7天、累计Spend不少于50元、曝光不少于1,000、商品点击不少于20，且prediction/material/版本关联完整。未通过时停止正式判断。

## 指标偏差

实际undefined不可评估；实际zero只记录未出值；实际positive才与正值P25–P75比较。区间内偏差分为0；低于P25或高于P75时用超出距离除以区间宽度。得分不大于0.5为轻微、0.5到1为中度、大于1为严重。P25=P75时使用最小容差。

schema v3的点预测误差使用`overall_point_prediction`；历史v2继续使用正值条件 P50，不回写历史。预测点为0且真实值大于0时相对误差为100%；预测点或真实值undefined时不计算；真实值为0时相对误差公式分母为0，只保留绝对误差和原因。

## 五维诊断

- Attraction：CTR Level。
- Conversion：CVR Level。
- Efficiency：70%结算ROI Level＋30%反向结算CPO Level；无结算订单为Level 1。
- Scale：40% Spend＋35% GMV＋25%订单Level。
- Quality：60%结算率＋40%低退款；无支付订单为unknown/level0。

预测命中只增加样本。正向超预期提炼可复制因素。负向建议必须有严重单项、低2级、同维多指标联动或Pattern转移证据。
