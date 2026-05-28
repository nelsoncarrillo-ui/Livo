// ── Chart.js defaults coherentes con el tema oscuro ──
if (window.Chart) {
  const isDark = document.documentElement.getAttribute('data-bs-theme') === 'dark';
  if (isDark) {
    Chart.defaults.color = '#93a1bd';
    Chart.defaults.borderColor = 'rgba(255,255,255,.06)';
    Chart.defaults.font.family = "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif";
    Chart.defaults.font.size = 11;
    if (Chart.defaults.plugins && Chart.defaults.plugins.legend) {
      Chart.defaults.plugins.legend.labels.color = '#cdd8ec';
    }
    if (Chart.defaults.plugins && Chart.defaults.plugins.tooltip) {
      Chart.defaults.plugins.tooltip.backgroundColor = '#0f1828';
      Chart.defaults.plugins.tooltip.borderColor = '#2b3650';
      Chart.defaults.plugins.tooltip.borderWidth = 1;
      Chart.defaults.plugins.tooltip.titleColor = '#fff';
      Chart.defaults.plugins.tooltip.bodyColor = '#cdd8ec';
      Chart.defaults.plugins.tooltip.padding = 10;
    }
    // Paleta por defecto agradable para datasets sin color explícito
    Chart.defaults.backgroundColor = ['#4f8cff','#22c55e','#f59e0b','#a78bfa','#ec4899','#06b6d4'];
  }
}

// Paleta reutilizable (para usar en templates: window.LIVO_COLORS)
window.LIVO_COLORS = {
  blue:'#4f8cff', green:'#22c55e', amber:'#f59e0b', purple:'#a78bfa',
  pink:'#ec4899', cyan:'#06b6d4', red:'#ef4444', slate:'#64748b',
  grid:'rgba(255,255,255,.06)', text:'#93a1bd'
};

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
