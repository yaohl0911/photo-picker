// 对比决策页占位（Task 11 实现：并排同步缩放对比 + 评分条 + 快捷键决策）。
import { renderPlaceholder } from './placeholder.js';

export function renderCompare(container) {
  return renderPlaceholder(container, {
    title: '对比决策',
    description:
      '并排对比组内照片：同步缩放查看 100% 像素细节、EXIF 与质量评分，' +
      '勾选要保留的照片后确认，进入下一组。',
    plannedTask: 'Task 11 实现',
  });
}
