// 历史/撤销页占位（Task 12 实现：已处理组列表 + 撤销淘汰移动）。
import { renderPlaceholder } from './placeholder.js';

export function renderHistory(container) {
  return renderPlaceholder(container, {
    title: '历史',
    description:
      '浏览已处理的组与决策记录，查看淘汰照片的去向，需要时一键撤销移动。',
    plannedTask: 'Task 12 实现',
  });
}
