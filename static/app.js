// Site-wide behaviour (kept in a file so the CSP can forbid inline scripts).
document.addEventListener('change', e => {
  const t = e.target;
  if (t.matches('[data-autosubmit]')) t.form.submit();
  if (t.matches('[data-newaddr]')) {
    const box = document.getElementById('newaddr');
    if (box) box.hidden = t.dataset.newaddr === 'hide';
  }
});

document.addEventListener('submit', e => {
  const msg = e.target.dataset.confirm;
  if (msg && !confirm(msg)) e.preventDefault();
});

// product gallery: switch between photos and videos
document.querySelectorAll('.thumb-btn').forEach(b => b.addEventListener('click', () => {
  const stage = document.getElementById('stage');
  if (!stage) return;
  document.querySelectorAll('.thumb-btn.on').forEach(x => x.classList.remove('on'));
  b.classList.add('on');
  stage.innerHTML = '';
  const el = document.createElement(b.dataset.kind === 'video' ? 'video' : 'img');
  el.src = b.dataset.src;
  if (el.tagName === 'VIDEO') { el.controls = true; el.playsInline = true; el.autoplay = true; } else { el.alt = ''; }
  stage.appendChild(el);
}));
