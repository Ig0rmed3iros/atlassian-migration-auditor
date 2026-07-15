(function () {
  const boxes = Array.from(document.querySelectorAll('.fixbox'));
  const hidden = document.getElementById('fix_ids');
  const confirmWf = document.getElementById('confirm-wf');
  const preview = document.getElementById('preview');
  if (!hidden || !confirmWf || !preview) return;
  function sync() {
    const on = boxes.filter(b => b.checked);
    hidden.value = on.map(b => b.value).join(',');
    const needsConfirm = on.some(b => b.dataset.confirm === 'True');
    confirmWf.style.display = needsConfirm ? 'block' : 'none';
    preview.textContent = on.length
      ? `${on.length} fix(es) selected: ` + on.map(b => b.value).join(', ')
      : 'Nothing selected.';
  }
  boxes.forEach(b => b.addEventListener('change', sync));
  sync();
})();
