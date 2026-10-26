// Product form: drag & drop uploads, preview, reorder, progress bar.
(function () {
  const form = document.querySelector('form[enctype]');
  const dz = document.getElementById('dropzone');
  if (!form || !dz) return;
  const input = document.getElementById('mediaInput'), grid = document.getElementById('newfiles'),
        existing = document.getElementById('existing'), msg = document.getElementById('uploadMsg'),
        bar = document.getElementById('progress'), MB = 1024 * 1024;
  const dt = new DataTransfer();
  const imagesOnly = dz.hasAttribute('data-images-only'), maxFiles = parseInt(dz.dataset.max || '0', 10);
  const isVideo = f => f.type.startsWith('video/') || /\.(mp4|webm|mov|m4v|ogg)$/i.test(f.name);
  const isImage = f => f.type.startsWith('image/') || /\.(png|jpe?g|webp|gif)$/i.test(f.name);

  function setFiles(files) {
    dt.items.clear();
    files.forEach(f => dt.items.add(f));
    input.files = dt.files;
    render();
  }
  function addFiles(list) {
    msg.textContent = '';
    const files = [...dt.files];
    for (const f of list) {
      if (!isVideo(f) && !isImage(f)) { msg.textContent = f.name + ': only images and videos are allowed.'; continue; }
      if (imagesOnly && !isImage(f)) { msg.textContent = f.name + ': only photos are allowed here.'; continue; }
      if (maxFiles && files.length + (existing ? existing.children.length : 0) >= maxFiles) { msg.textContent = 'You can add up to ' + maxFiles + ' photos.'; break; }
      if (isVideo(f) ? f.size > 100 * MB : f.size > 10 * MB) { msg.textContent = f.name + ': too large (images 10 MB, videos 100 MB).'; continue; }
      if (files.some(x => x.name === f.name && x.size === f.size)) continue;
      files.push(f);
    }
    setFiles(files);
  }
  function render() {
    grid.innerHTML = '';
    [...dt.files].forEach((f, i) => {
      const d = document.createElement('div');
      d.className = 'mitem new';
      const url = URL.createObjectURL(f);
      d.innerHTML = isVideo(f)
        ? '<video src="' + url + '#t=0.1" preload="metadata" muted></video><span class="vbadge">▶</span>'
        : '<img src="' + url + '" alt="">';
      const x = document.createElement('button');
      x.type = 'button'; x.className = 'x'; x.textContent = '×'; x.title = 'Remove';
      x.onclick = () => setFiles([...dt.files].filter((_, j) => j !== i));
      d.appendChild(x);
      grid.appendChild(d);
    });
  }

  dz.addEventListener('click', () => input.click());
  dz.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); } });
  input.addEventListener('change', () => addFiles([...input.files]));
  ['dragenter', 'dragover'].forEach(ev => dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.add('over'); }));
  ['dragleave', 'drop'].forEach(ev => dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.remove('over'); }));
  dz.addEventListener('drop', e => addFiles([...e.dataTransfer.files]));
  // don't let the browser navigate to a file dropped outside the zone
  ['dragover', 'drop'].forEach(ev => window.addEventListener(ev, e => { if (!dz.contains(e.target)) e.preventDefault(); }));

  if (existing) {  // reorder / remove files already saved
    let drag = null;
    existing.addEventListener('click', e => { if (e.target.classList.contains('x')) e.target.closest('.mitem').remove(); });
    existing.addEventListener('dragstart', e => {
      drag = e.target.closest('.mitem');
      if (!drag) return;
      drag.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', '');
    });
    existing.addEventListener('dragend', () => { if (drag) drag.classList.remove('dragging'); drag = null; });
    existing.addEventListener('dragover', e => {
      if (!drag) return;
      e.preventDefault(); e.stopPropagation();
      const over = e.target.closest('.mitem');
      if (!over || over === drag) return;
      const r = over.getBoundingClientRect();
      existing.insertBefore(drag, (e.clientX - r.left) > r.width / 2 ? over.nextSibling : over);
    });
  }

  form.addEventListener('submit', e => {
    e.preventDefault();
    msg.textContent = '';
    const btn = form.querySelector('button.btn');
    btn.disabled = true; btn.textContent = 'Saving…';
    const fail = m => { msg.textContent = m; btn.disabled = false; btn.textContent = 'Save'; bar.hidden = true; };
    const xhr = new XMLHttpRequest();
    xhr.open('POST', location.href);
    xhr.setRequestHeader('X-Requested-With', 'fetch');
    if (dt.files.length) {
      bar.hidden = false;
      xhr.upload.onprogress = ev => { if (ev.lengthComputable) bar.firstElementChild.style.width = (ev.loaded / ev.total * 100) + '%'; };
    }
    xhr.onload = () => {
      let j = {};
      try { j = JSON.parse(xhr.responseText); } catch (_) {}
      if (j.redirect) { location.href = j.redirect; return; }
      if (j.error) return fail(j.error);
      if (xhr.status === 401 || xhr.status === 403) return fail('You are not logged in as admin. Log in again.');
      if (xhr.status === 400) return fail('Your session expired. Reload this page and try again.');
      if (xhr.status >= 500) return fail('Server error (' + xhr.status + '). The upload may have timed out or been too large; check Admin → Diagnostics.');
      fail('Save failed (' + xhr.status + ').');
    };
    xhr.onerror = () => fail('Connection lost while uploading. The file may be too large or the server restarted. Try fewer/smaller files.');
    xhr.ontimeout = xhr.onerror;
    xhr.send(new FormData(form));
  });
})();
