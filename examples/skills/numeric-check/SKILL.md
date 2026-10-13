---
name: 数值核对
description: 核对论文中的数值：百分比变化、比值、描述性统计与极值定位
version: 1.0.0
author: paperopenclaw
tools:
  - name: numeric_check
    description: 对一组数值计算描述性统计，并计算指定两点的百分比变化与比值，用于核对论文中的提升幅度
    type: script
    script: scripts/numeric_check.py
    timeout: 60
    parameters:
      type: object
      properties:
        values:
          type: array
          items: { type: number }
          description: 待核对的数值序列
        labels:
          type: array
          items: { type: string }
          description: 与 values 一一对应的标签，可选
        compare:
          type: array
          items: { type: number }
          description: 需要计算百分比变化与比值的两点，例如 [25.8, 28.4] 表示从 25.8 提升到 28.4
      required: [values]
  - name: metric_delta
    description: 只计算两个数值之间的差值、百分比变化与倍数，适合快速核对「提升 X%」的声称
    type: script
    script: scripts/numeric_check.py
    timeout: 30
    parameters:
      type: object
      properties:
        before: { type: number, description: 变化前的数值 }
        after: { type: number, description: 变化后的数值 }
      required: [before, after]
---

# 使用说明

当需要核对论文中出现的数值声称时使用本技能。

## 适用场景

- 作者声称「提升了 10%」，需要确认基准与结果之间的实际差距；
- 需要判断某个指标是绝对提升还是相对提升；
- 需要一组实验结果的均值、极值与离散程度；
- 需要确认表格中的最大值对应哪一个标签。

## 使用要求

1. 调用 **numeric_check** 时，`values` 必须原样来自论文，不要自行四舍五入；
   标签按论文中的顺序传入，便于结果回指。
2. 只有在需要核对某个具体声称时才传 `compare`，避免无意义计算。
3. 得到结果后，必须把结论与原文数值一起引用，并明确说明是绝对变化还是相对变化。
   例如：「BLEU 由 25.8 提升至 28.4，相对提升约 10.1%，绝对提升 2.6」。
4. 本技能只做算术，不做推断。如果论文的声称需要统计显著性判断，
   应指出本技能无法验证，需要论文提供方差或置信区间。

## 输出解读

- `change.percent` 为相对变化百分比，`change.absolute` 为绝对差值；
- `spread.range` 为极差，`spread.std` 为总体标准差；
- `extremes.max_label` 在提供了 labels 时给出最大值对应的标签。
