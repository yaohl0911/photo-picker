// 总览页占位（Task 11 实现：分组网格卡片 + 状态筛选 + 进度统计）。
import { renderPlaceholder } from './placeholder.js';

export function renderOverview(container) {
  return renderPlaceholder(container, {
    title: '分组总览',
    description:
      '按相似组浏览整个照片库：每组一张推荐缩略图、组内张数与决策状态，' +
      '支持待处理 / 已完成筛选，随时进入对比决策。',
    plannedTask: 'Task 11 实现',
  });
}
