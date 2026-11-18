// 偏好面板占位（Task 12 实现：权重可视化 + 微调/锁定 + 学习进度）。
import { renderPlaceholder } from './placeholder.js';

export function renderPreferences(container) {
  return renderPlaceholder(container, {
    title: '偏好',
    description:
      '查看偏好模型对各质量维度的权重、学习样本数与变化趋势，' +
      '可手动微调或锁定某个维度。',
    plannedTask: 'Task 12 实现',
  });
}
