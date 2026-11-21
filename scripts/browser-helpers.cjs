// Shared UI helpers: all acceptance scripts use the published-date calendar.
async function selectCalendarDate(page, assetId) {
  const calendar = page.getByRole('combobox', { name: '浏览月份' });
  if (!(await calendar.isVisible())) await page.getByRole('button', { name: '展开变量与日期' }).click();
  const filter = page.getByRole('checkbox', { name: '优先显示高覆盖日期' });
  if (await filter.isChecked()) await filter.uncheck();
  const date = assetId.slice(-10);
  await calendar.selectOption(date.slice(0, 7));
  await page.locator('[data-date="' + date + '"]').click();
}
async function getSelectedId(page) {
  return page.evaluate(() => document.querySelector('#app').__vue_app__._instance.setupState.selectedId);
}
module.exports = { selectCalendarDate, getSelectedId };
