// Upload zone drag-and-drop
document.addEventListener('DOMContentLoaded', () => {
  const zone = document.getElementById('uploadZone');
  const input = document.getElementById('fileInput');
  const label = document.getElementById('fileName');

  if (!zone || !input) return;

  zone.addEventListener('click', () => input.click());

  input.addEventListener('change', () => {
    if (input.files.length) {
      label.textContent = '✓ ' + input.files[0].name;
    }
  });

  zone.addEventListener('dragover', e => {
    e.preventDefault();
    zone.classList.add('dragover');
  });

  zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));

  zone.addEventListener('drop', e => {
    e.preventDefault();
    zone.classList.remove('dragover');
    const files = e.dataTransfer.files;
    if (files.length) {
      input.files = files;
      label.textContent = '✓ ' + files[0].name;
    }
  });
});
