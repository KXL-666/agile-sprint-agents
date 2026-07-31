document.querySelectorAll('[data-confirm]').forEach((button) => {
  button.addEventListener('click', (event) => {
    if (!window.confirm(button.dataset.confirm)) event.preventDefault();
  });
});

document.querySelectorAll('[data-go-back]').forEach((button) => {
  button.addEventListener('click', () => {
    if (window.history.length > 1) window.history.back();
    else window.location.assign('/');
  });
});

const chatThread = document.querySelector('.chat-thread');
if (chatThread) {
  chatThread.scrollTop = chatThread.scrollHeight;
}

document.querySelectorAll('[data-folder-picker], [data-file-picker]').forEach((button) => {
  button.addEventListener('click', async () => {
    const original = button.textContent;
    button.disabled = true;
    button.textContent = '正在打开…';
    try {
      const endpoint = button.hasAttribute('data-folder-picker') ? '/workspace-picker' : '/file-picker';
      const response = await fetch(endpoint, {method: 'POST', credentials: 'same-origin'});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || '无法打开选择器');
      if (data.path) document.getElementById(button.dataset.target).value = data.path;
    } catch (error) {
      window.alert(error.message || '无法打开 Windows 文件选择器');
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  });
});

const progressPanel = document.getElementById('model-progress-panel');
const stopModelWorkButton = document.getElementById('stop-model-work');
let modelProgressTimer;
let activeSprintId = null;
function closeModelProgress() {
  if (progressPanel) progressPanel.hidden = true;
  if (stopModelWorkButton) stopModelWorkButton.hidden = true;
  activeSprintId = null;
  if (modelProgressTimer) window.clearInterval(modelProgressTimer);
}
document.querySelectorAll('[data-close-model-progress]').forEach((button) => {
  button.addEventListener('click', closeModelProgress);
});
document.querySelectorAll('form[data-model-run]').forEach((form) => {
  form.addEventListener('submit', () => {
    const button = form.querySelector('button');
    if (button) { button.disabled = true; button.textContent = '正在提交…'; }
    if (!progressPanel) return;
    const pathMatch = new URL(form.action, window.location.href).pathname.match(/\/sprints\/(\d+)\//);
    activeSprintId = pathMatch ? pathMatch[1] : null;
    if (stopModelWorkButton) {
      stopModelWorkButton.hidden = !activeSprintId;
      stopModelWorkButton.disabled = false;
      stopModelWorkButton.textContent = '停止本次工作';
    }
    progressPanel.hidden = false;
    const title = document.getElementById('model-progress-title');
    const stages = ['项目经理正在理解需求', '开发 Agent 正在评估任务', '项目经理正在协调分工', '正在生成待审批文件提案'];
    const elapsed = document.getElementById('model-progress-elapsed');
    let index = 0;
    let seconds = 0;
    if (modelProgressTimer) window.clearInterval(modelProgressTimer);
    modelProgressTimer = window.setInterval(() => {
      index = Math.min(index + 1, stages.length - 1);
      if (title) title.textContent = stages[index];
      seconds += 1;
      if (elapsed) elapsed.textContent = `已等待 ${seconds} 秒`;
    }, 1000);
  });
});

stopModelWorkButton?.addEventListener('click', async () => {
  if (!activeSprintId) return;
  stopModelWorkButton.disabled = true;
  stopModelWorkButton.textContent = '正在停止…';
  try {
    const response = await fetch(`/sprints/${activeSprintId}/stop`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {'X-Requested-With': 'XMLHttpRequest'},
    });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.message || '无法停止本次工作');
    const title = document.getElementById('model-progress-title');
    const elapsed = document.getElementById('model-progress-elapsed');
    if (title) title.textContent = '已请求停止当前工作';
    if (elapsed) elapsed.textContent = '当前一步结束后将停止';
    stopModelWorkButton.textContent = '已请求停止';
  } catch (error) {
    stopModelWorkButton.disabled = false;
    stopModelWorkButton.textContent = '重试停止';
    window.alert(error.message || '停止请求未成功，请稍后重试。');
  }
});

const approvalDrawer = document.getElementById('file-approval-drawer');
const workspacePanelLabels = {
  files: ['文件审批', '批准后才会写入本地文件'],
  tests: ['运行测试', '在当前任务内查看测试结果'],
  tasks: ['任务列表', '项目经理拆解并分配的工作'],
  reports: ['报告与导出', '生成本次任务的交付材料'],
};
function openApprovalDrawer(panel = 'files') {
  if (!approvalDrawer) return;
  const labels = workspacePanelLabels[panel] || workspacePanelLabels.files;
  approvalDrawer.querySelectorAll('[data-workspace-content]').forEach((content) => {
    content.hidden = content.dataset.workspaceContent !== panel;
  });
  const title = document.getElementById('workspace-drawer-title');
  const subtitle = document.getElementById('workspace-drawer-subtitle');
  if (title) title.textContent = labels[0];
  if (subtitle) subtitle.textContent = labels[1];
  approvalDrawer.hidden = false;
  window.requestAnimationFrame(() => approvalDrawer.classList.add('is-open'));
  window.history.replaceState(null, '', `#${panel}`);
  approvalDrawer.querySelector('button, summary')?.focus();
}
function closeApprovalDrawer() {
  if (!approvalDrawer) return;
  approvalDrawer.classList.remove('is-open');
  window.setTimeout(() => { approvalDrawer.hidden = true; }, 180);
  if (window.location.hash === '#file-approval-drawer') window.history.replaceState(null, '', window.location.pathname + window.location.search);
}
document.querySelectorAll('[data-open-approval-drawer]').forEach((button) => button.addEventListener('click', () => openApprovalDrawer('files')));
document.querySelectorAll('[data-workspace-panel]').forEach((button) => button.addEventListener('click', () => {
  const panel = button.dataset.workspacePanel;
  if (panel === 'chat') {
    closeApprovalDrawer();
    document.querySelector('#message')?.focus();
    return;
  }
  document.querySelectorAll('[data-workspace-panel]').forEach((item) => item.classList.toggle('active', item.dataset.workspacePanel === panel));
  openApprovalDrawer(panel);
}));
document.querySelectorAll('[data-close-approval-drawer]').forEach((button) => button.addEventListener('click', closeApprovalDrawer));
if (approvalDrawer && ['#file-approval-drawer', '#files', '#tests', '#tasks', '#reports'].includes(window.location.hash)) {
  openApprovalDrawer(window.location.hash === '#file-approval-drawer' ? 'files' : window.location.hash.slice(1));
}

const timelineEntries = [...document.querySelectorAll('.timeline-entry')];
const timelineEmpty = document.querySelector('.timeline-empty');
let selectedRole = 'all';
let selectedStage = 'all';
function applyTimelineFilters() {
  let visible = 0;
  timelineEntries.forEach((entry) => {
    const match = (selectedRole === 'all' || entry.dataset.role === selectedRole) && (selectedStage === 'all' || entry.dataset.stage === selectedStage);
    entry.hidden = !match;
    if (match) visible += 1;
  });
  if (timelineEmpty) timelineEmpty.hidden = visible !== 0;
}
document.querySelectorAll('[data-timeline-filter]').forEach((button) => {
  button.addEventListener('click', () => {
    selectedRole = button.dataset.timelineFilter;
    document.querySelectorAll('[data-timeline-filter]').forEach((item) => item.classList.toggle('active', item === button));
    applyTimelineFilters();
  });
});
document.querySelector('.timeline-stage-select')?.addEventListener('change', (event) => {
  selectedStage = event.target.value;
  applyTimelineFilters();
});

const selectAll = document.querySelector('[data-select-all]');
selectAll?.addEventListener('change', () => {
  document.querySelectorAll('[data-operation-check]').forEach((checkbox) => { checkbox.checked = selectAll.checked; });
});

const projectSelect = document.getElementById('project_id');
function updateVisibleTeam() {
  if (!projectSelect) return;
  document.querySelectorAll('[data-project-members]').forEach((group) => {
    group.hidden = group.dataset.projectMembers !== projectSelect.value;
  });
}
projectSelect?.addEventListener('change', updateVisibleTeam);

const presetValue = document.getElementById('team-preset-value');
document.querySelectorAll('[data-team-preset]').forEach((button) => {
  button.addEventListener('click', () => {
    const preset = button.dataset.teamPreset;
    presetValue.value = preset;
    document.querySelectorAll('[data-team-preset]').forEach((item) => item.classList.toggle('active', item === button));
    const visible = [...document.querySelectorAll('[data-project-members]:not([hidden]) input[type="checkbox"]')];
    const selectedCount = preset === 'quick' ? 1 : preset === 'deep' ? visible.length : Math.min(2, visible.length);
    visible.forEach((checkbox, index) => { checkbox.checked = index < selectedCount; });
  });
});

document.querySelectorAll('[data-prompt-example]').forEach((button) => {
  button.addEventListener('click', () => {
    const goal = document.getElementById('goal');
    if (goal) { goal.value = button.dataset.promptExample; goal.focus(); }
  });
});

const feed = document.querySelector('[data-reveal-feed]');
if (feed) {
  const items = [...feed.querySelectorAll('[data-feed-item]')];
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  items.forEach((item, index) => {
    window.setTimeout(() => {
      item.hidden = false;
      item.classList.add('is-visible');
      if (index === items.length - 1) item.scrollIntoView({block: 'nearest', behavior: reduceMotion ? 'auto' : 'smooth'});
    }, reduceMotion ? 0 : index * 600);
  });
}
