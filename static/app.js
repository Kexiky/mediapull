/**
 * MediaPull by Kexiky - Client Application Logic
 */

let currentVideoInfo = null;
let currentTaskId = null;
let activeEventSource = null;
let activeMode = "video"; // "video" or "audio"

let currentUser = {
  user_id: "guest",
  is_creator: false,
  is_permanent: false,
  ttl_hours: 24
};

// Initialize on page load
document.addEventListener("DOMContentLoaded", () => {
  // Initialize theme & mode system
  initAppTheme();

  const urlInput = document.getElementById("videoUrlInput");
  const btnClear = document.getElementById("btnClear");

  if (urlInput && btnClear) {
    urlInput.addEventListener("input", () => {
      btnClear.style.display = urlInput.value.trim() ? "flex" : "none";
    });
  }

  // Check auth and user tier
  checkAuthStatus();

  // Load history count badge
  loadHistoryFiles(true);

  // Initialize Unified Real-Time Progress Stream (Single persistent SSE socket)
  initGlobalProgressStream();

  // Load existing and active tasks into downloads queue
  loadTasksQueue();
});

// Authentication & Profile Logic
async function checkAuthStatus() {
  try {
    const res = await fetch("/api/auth/me");
    if (res.ok) {
      currentUser = await res.json();
      updateProfileUI();
    }
  } catch (e) {
    console.error("Auth check failed:", e);
  }
}

function updateProfileUI() {
  const avatar = document.getElementById("userProfileAvatar");
  const name = document.getElementById("userProfileName");
  const tier = document.getElementById("userProfileTier");
  const btn = document.getElementById("btnUserProfile");
  if (!btn) return;

  if (currentUser && currentUser.role !== "guest") {
    btn.className = `btn-navbar-profile ${currentUser.role_badge_class || 'user'}`;
    if (avatar) avatar.innerText = currentUser.role_icon || "👤";
    if (name) name.innerText = currentUser.username;
    if (tier) tier.innerText = currentUser.is_permanent ? "♾️ Вечно" : (currentUser.ttl_display || "⏳ 7д");
    btn.title = `${currentUser.role_title}: ${currentUser.username} • Нажмите для перехода в личный кабинет`;
  } else {
    btn.className = "btn-navbar-profile guest";
    if (avatar) avatar.innerText = "👤";
    const shortId = currentUser?.user_id?.replace("guest_", "").slice(0, 5) || "гость";
    if (name) name.innerText = `Гость (#${shortId})`;
    if (tier) tier.innerText = "⏳ 24ч";
    btn.title = "Гостевой режим (файлы 24 часа). Нажмите для входа или регистрации.";
  }
}

function handleUserProfileClick() {
  if (currentUser && currentUser.role !== "guest") {
    openCabinetModal();
  } else {
    openAuthModal('login');
  }
}

// Unified Auth Modal (Login / Register)
function openAuthModal(tab = 'login') {
  const modal = document.getElementById("authModal");
  if (!modal) return;
  switchAuthTab(tab);
  modal.style.display = "flex";
  setTimeout(() => {
    if (tab === 'login') {
      document.getElementById("loginUsernameInput")?.focus();
    } else {
      document.getElementById("regUsernameInput")?.focus();
    }
  }, 100);
}

function closeAuthModal() {
  const modal = document.getElementById("authModal");
  if (modal) modal.style.display = "none";
}

function switchAuthTab(tab) {
  const btnLogin = document.getElementById("tabBtnLogin");
  const btnRegister = document.getElementById("tabBtnRegister");
  const formLogin = document.getElementById("authLoginForm");
  const formRegister = document.getElementById("authRegisterForm");
  const title = document.getElementById("authModalTitle");
  const badge = document.getElementById("authModalBadge");
  const errLogin = document.getElementById("loginErrorMsg");
  const errReg = document.getElementById("regErrorMsg");
  if (errLogin) errLogin.style.display = "none";
  if (errReg) errReg.style.display = "none";

  if (tab === 'login') {
    btnLogin?.classList.add("active");
    btnRegister?.classList.remove("active");
    if (formLogin) formLogin.style.display = "block";
    if (formRegister) formRegister.style.display = "none";
    if (title) title.innerText = "Вход в аккаунт";
    if (badge) badge.innerText = "🔐";
  } else {
    btnRegister?.classList.add("active");
    btnLogin?.classList.remove("active");
    if (formRegister) formRegister.style.display = "block";
    if (formLogin) formLogin.style.display = "none";
    if (title) title.innerText = "Регистрация";
    if (badge) badge.innerText = "✨";
  }
}

async function submitLogin() {
  const userInput = document.getElementById("loginUsernameInput");
  const passInput = document.getElementById("loginPasswordInput");
  const errorMsg = document.getElementById("loginErrorMsg");
  const username = userInput ? userInput.value.trim() : "";
  const password = passInput ? passInput.value.trim() : "";

  if (!password) {
    if (errorMsg) {
      errorMsg.innerText = "Введите пароль";
      errorMsg.style.display = "block";
    }
    return;
  }

  try {
    const res = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password })
    });
    if (res.ok) {
      const data = await res.json();
      closeAuthModal();
      await checkAuthStatus();
      showToast(`С возвращением, ${data.username}! Вы вошли как ${data.role_title} ${data.role_icon}`, "success");
      loadHistoryFiles(true);
    } else {
      const err = await res.json();
      if (errorMsg) {
        errorMsg.innerText = err.detail || "Неверный логин или пароль";
        errorMsg.style.display = "block";
      }
    }
  } catch (e) {
    if (errorMsg) {
      errorMsg.innerText = "Ошибка соединения с сервером";
      errorMsg.style.display = "block";
    }
  }
}

async function submitRegister() {
  const userInput = document.getElementById("regUsernameInput");
  const passInput = document.getElementById("regPasswordInput");
  const passConfInput = document.getElementById("regPasswordConfirmInput");
  const errorMsg = document.getElementById("regErrorMsg");

  const username = userInput ? userInput.value.trim() : "";
  const password = passInput ? passInput.value.trim() : "";
  const passwordConfirm = passConfInput ? passConfInput.value.trim() : "";

  if (!username) {
    if (errorMsg) {
      errorMsg.innerText = "Введите имя пользователя";
      errorMsg.style.display = "block";
    }
    return;
  }
  if (!password) {
    if (errorMsg) {
      errorMsg.innerText = "Введите пароль (от 4 символов)";
      errorMsg.style.display = "block";
    }
    return;
  }
  if (password !== passwordConfirm) {
    if (errorMsg) {
      errorMsg.innerText = "Пароли не совпадают";
      errorMsg.style.display = "block";
    }
    return;
  }

  try {
    const res = await fetch("/api/auth/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password })
    });
    if (res.ok) {
      const data = await res.json();
      closeAuthModal();
      await checkAuthStatus();
      showToast(`Регистрация успешна! Добро пожаловать, ${data.username} 🎉`, "success");
      loadHistoryFiles(true);
    } else {
      const err = await res.json();
      if (errorMsg) {
        errorMsg.innerText = err.detail || "Ошибка при регистрации";
        errorMsg.style.display = "block";
      }
    }
  } catch (e) {
    if (errorMsg) {
      errorMsg.innerText = "Ошибка соединения с сервером";
      errorMsg.style.display = "block";
    }
  }
}

// Personal Cabinet Modal
async function openCabinetModal(tab = 'storage') {
  const modal = document.getElementById("cabinetModal");
  if (!modal) return;

  // Refresh current user info and usage
  await checkAuthStatus();

  // Populate Cabinet Header
  const avatarIcon = document.getElementById("cabinetAvatarIcon");
  const userText = document.getElementById("cabinetUsernameText");
  const roleBadge = document.getElementById("cabinetRoleBadge");
  const regDate = document.getElementById("cabinetRegDateText");
  const adminTabBtn = document.getElementById("tabBtnCabAdmin");

  if (avatarIcon) avatarIcon.innerText = currentUser?.role_icon || "👤";
  if (userText) userText.innerText = currentUser?.username || "Пользователь";
  if (roleBadge) {
    roleBadge.className = `role-badge ${currentUser?.role_badge_class || 'user'}`;
    roleBadge.innerText = currentUser?.role_title || "Пользователь";
  }
  if (regDate) {
    if (currentUser?.created_at) {
      const d = new Date(currentUser.created_at * 1000);
      regDate.innerText = `Регистрация: ${d.toLocaleDateString('ru-RU')}`;
    } else {
      regDate.innerText = "Регистрация: бессрочный аккаунт";
    }
  }

  // Populate Tab 1 Storage
  const quotaText = document.getElementById("cabinetQuotaText");
  const quotaBar = document.getElementById("cabinetQuotaBar");
  const metricTtl = document.getElementById("cabinetMetricTtl");
  const metricFiles = document.getElementById("cabinetMetricFiles");
  const metricSize = document.getElementById("cabinetMetricSize");
  const metricQuality = document.getElementById("cabinetMetricQuality");

  if (metricTtl) metricTtl.innerText = currentUser?.ttl_display || "7 дней";
  if (metricFiles) metricFiles.innerText = currentUser?.file_count || 0;
  if (metricSize) metricSize.innerText = currentUser?.used_size_str || "0 B";

  if (currentUser?.is_permanent || !currentUser?.storage_limit_bytes) {
    if (quotaText) quotaText.innerText = `${currentUser?.used_size_str || '0 B'} / Безлимитно (♾️)`;
    if (quotaBar) {
      quotaBar.style.width = "100%";
      quotaBar.className = "quota-progress-fill unlimited";
    }
    if (metricQuality) metricQuality.innerText = "до 8K Ultra HD";
  } else {
    const used = currentUser.used_bytes || 0;
    const limit = currentUser.storage_limit_bytes;
    const pct = Math.min(100, Math.round((used / limit) * 100));
    if (quotaText) quotaText.innerText = `${currentUser.used_size_str || '0 B'} из ${currentUser.storage_limit_display} (${pct}%)`;
    if (quotaBar) {
      quotaBar.style.width = `${pct}%`;
      quotaBar.className = `quota-progress-fill ${pct > 90 ? 'danger' : (pct > 75 ? 'warning' : '')}`;
    }
    if (metricQuality) metricQuality.innerText = currentUser.role === 'premium' ? 'до 4K UHD' : 'до 1080p Full HD';
  }

  // Populate API Token
  const tokenInput = document.getElementById("cabApiTokenInput");
  if (tokenInput) {
    tokenInput.value = currentUser?.token || (currentUser?.username ? `mp_token_${currentUser.username}` : "");
  }

  // Show/hide admin tab
  if (adminTabBtn) {
    if (currentUser?.is_admin) {
      adminTabBtn.style.display = "flex";
    } else {
      adminTabBtn.style.display = "none";
    }
  }

  switchCabinetTab(tab);
  modal.style.display = "flex";
}

function closeCabinetModal() {
  const modal = document.getElementById("cabinetModal");
  if (modal) modal.style.display = "none";
}

function switchCabinetTab(tab) {
  const btnStorage = document.getElementById("tabBtnCabStorage");
  const btnSecurity = document.getElementById("tabBtnCabSecurity");
  const btnAdmin = document.getElementById("tabBtnCabAdmin");
  const secStorage = document.getElementById("cabTabContentStorage");
  const secSecurity = document.getElementById("cabTabContentSecurity");
  const secAdmin = document.getElementById("cabTabContentAdmin");

  btnStorage?.classList.toggle("active", tab === 'storage');
  btnSecurity?.classList.toggle("active", tab === 'security');
  btnAdmin?.classList.toggle("active", tab === 'admin');

  if (secStorage) secStorage.style.display = tab === 'storage' ? 'block' : 'none';
  if (secSecurity) secSecurity.style.display = tab === 'security' ? 'block' : 'none';
  if (secAdmin) secAdmin.style.display = tab === 'admin' ? 'block' : 'none';

  if (tab === 'admin' && currentUser?.is_admin) {
    loadAdminData();
  }
}

async function submitChangePassword() {
  const oldInput = document.getElementById("cabOldPassInput");
  const newInput = document.getElementById("cabNewPassInput");
  const err = document.getElementById("cabPassErrorMsg");
  const ok = document.getElementById("cabPassSuccessMsg");
  if (err) err.style.display = "none";
  if (ok) ok.style.display = "none";

  const old_password = oldInput ? oldInput.value.trim() : "";
  const new_password = newInput ? newInput.value.trim() : "";

  if (!old_password || !new_password) {
    if (err) {
      err.innerText = "Заполните оба поля";
      err.style.display = "block";
    }
    return;
  }

  try {
    const res = await fetch("/api/auth/change-password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ old_password, new_password })
    });
    if (res.ok) {
      if (ok) {
        ok.innerText = "Пароль успешно обновлен!";
        ok.style.display = "block";
      }
      if (oldInput) oldInput.value = "";
      if (newInput) newInput.value = "";
    } else {
      const data = await res.json();
      if (err) {
        err.innerText = data.detail || "Ошибка при смене пароля";
        err.style.display = "block";
      }
    }
  } catch (e) {
    if (err) {
      err.innerText = "Ошибка соединения с сервером";
      err.style.display = "block";
    }
  }
}

function copyApiToken() {
  const tokenInput = document.getElementById("cabApiTokenInput");
  const copyBtn = document.getElementById("btnCopyApiToken");
  if (tokenInput && tokenInput.value) {
    const onCopied = () => {
      showToast("API-токен скопирован в буфер обмена!", "success");
      if (copyBtn) {
        const prevHtml = copyBtn.innerHTML;
        copyBtn.innerHTML = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"></polyline></svg> <span>Скопировано!</span>`;
        copyBtn.style.background = "rgba(48, 209, 88, 0.18)";
        copyBtn.style.color = "#30d158";
        copyBtn.style.borderColor = "rgba(48, 209, 88, 0.45)";
        setTimeout(() => {
          copyBtn.innerHTML = prevHtml;
          copyBtn.style.background = "";
          copyBtn.style.color = "";
          copyBtn.style.borderColor = "";
        }, 1800);
      }
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(tokenInput.value).then(onCopied).catch(() => {
        tokenInput.select();
        document.execCommand("copy");
        onCopied();
      });
    } else {
      tokenInput.select();
      document.execCommand("copy");
      onCopied();
    }
  }
}

// Admin Panel Functions
async function loadAdminData() {
  try {
    const [statsRes, usersRes] = await Promise.all([
      fetch("/api/admin/system-stats"),
      fetch("/api/admin/users")
    ]);

    if (statsRes.ok) {
      const stats = await statsRes.json();
      const diskFree = document.getElementById("adminStatDiskFree");
      const dbSize = document.getElementById("adminStatDbSize");
      const usersCount = document.getElementById("adminStatUsersCount");
      if (diskFree) diskFree.innerText = `${stats.disk_free_str} своб.`;
      if (dbSize) dbSize.innerText = stats.db_size_str;
      if (usersCount) usersCount.innerText = stats.total_users;
    }

    if (usersRes.ok) {
      const data = await usersRes.json();
      renderAdminUsersTable(data.users || []);
    }
  } catch (e) {
    console.error("Failed to load admin data:", e);
  }
}

function renderAdminUsersTable(users) {
  const tbody = document.getElementById("adminUsersTableBody");
  if (!tbody) return;

  if (!users || users.length === 0) {
    tbody.innerHTML = `<tr><td colspan="5" style="text-align: center; color: var(--text-secondary); padding: 20px;">Пользователей не найдено</td></tr>`;
    return;
  }

  tbody.innerHTML = users.map(u => {
    const isOwner = u.username === "kexiky";
    const roleSelector = isOwner
      ? `<span class="role-badge admin" style="font-size: 0.72rem;">👑 Создатель</span>`
      : `
        <select class="admin-role-select" onchange="changeUserRole('${escapeHtml(u.username)}', this.value)">
          <option value="user" ${u.role === 'user' ? 'selected' : ''}>👤 Пользователь</option>
          <option value="premium" ${u.role === 'premium' ? 'selected' : ''}>⭐ Премиум</option>
          <option value="admin" ${u.role === 'admin' ? 'selected' : ''}>👑 Админ</option>
        </select>
      `;

    const deleteBtn = isOwner
      ? `<span style="color: var(--text-secondary); font-size: 0.8rem;">—</span>`
      : `
        <button type="button" class="btn-admin-icon" onclick="adminDeleteUser('${escapeHtml(u.username)}')" title="Удалить пользователя">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
        </button>
      `;

    return `
      <tr>
        <td style="color: var(--text-secondary); font-family: var(--font-mono);">${u.id}</td>
        <td style="font-weight: 600;">${escapeHtml(u.username)}</td>
        <td>${roleSelector}</td>
        <td style="font-family: var(--font-mono); font-size: 0.82rem;">${u.used_size_str || '0 B'} (${u.file_count || 0})</td>
        <td style="text-align: right;">${deleteBtn}</td>
      </tr>
    `;
  }).join("");
}

async function changeUserRole(username, newRole) {
  try {
    const res = await fetch(`/api/admin/users/${encodeURIComponent(username)}/role`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ role: newRole })
    });
    if (res.ok) {
      showToast(`Роль ${username} изменена на ${newRole} ✓`, "success");
      loadAdminData();
    } else {
      const err = await res.json();
      showToast(err.detail || "Ошибка при смене роли", "error");
    }
  } catch (e) {
    showToast("Ошибка соединения", "error");
  }
}

async function adminDeleteUser(username) {
  if (!confirm(`Удалить аккаунт пользователя '${username}'?`)) return;

  try {
    const res = await fetch(`/api/admin/users/${encodeURIComponent(username)}`, {
      method: "DELETE"
    });
    if (res.ok) {
      showToast(`Пользователь ${username} удален`, "info");
      loadAdminData();
    } else {
      const err = await res.json();
      showToast(err.detail || "Ошибка удаления пользователя", "error");
    }
  } catch (e) {
    showToast("Ошибка соединения", "error");
  }
}

async function triggerAdminCleanup() {
  try {
    const res = await fetch("/api/admin/cleanup", { method: "POST" });
    if (res.ok) {
      const data = await res.json();
      const r = data.result;
      showToast(`Очистка завершена: удалено ${r.deleted_files} файлов (освобождено ${r.freed_mb} МБ)`, "success");
      loadAdminData();
      loadHistoryFiles(true);
    } else {
      showToast("Ошибка при запуске очистки", "error");
    }
  } catch (e) {
    showToast("Ошибка соединения", "error");
  }
}

async function submitLogout() {
  try {
    await fetch("/api/auth/logout", { method: "POST" });
    closeCabinetModal();
    showToast("Вы вышли из учетной записи", "info");
    await checkAuthStatus();
    loadHistoryFiles(true);
  } catch (e) {
    console.error("Logout error:", e);
  }
}

// Toast Notifications
function showToast(message, type = "info", duration = 3500) {
  const container = document.getElementById("toastContainer");
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.innerText = message;
  container.appendChild(toast);

  setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.transform = "translateX(50px)";
    toast.style.transition = "all 0.3s ease";
    setTimeout(() => toast.remove(), 300);
  }, duration);
}

// Tab Switching
function switchMainTab(tab) {
  const tabDownloader = document.getElementById("tabDownloader");
  const tabHistory = document.getElementById("tabHistory");
  const btnDownloader = document.getElementById("tabBtnDownloader");
  const btnHistory = document.getElementById("tabBtnHistory");

  if (tab === "downloader") {
    tabDownloader.style.display = "block";
    tabHistory.style.display = "none";
    btnDownloader.classList.add("active");
    btnHistory.classList.remove("active");
  } else {
    tabDownloader.style.display = "none";
    tabHistory.style.display = "block";
    btnDownloader.classList.remove("active");
    btnHistory.classList.add("active");
    loadHistoryFiles();
  }
}

// Clipboard Paste
async function handlePasteClipboard() {
  try {
    const text = await navigator.clipboard.readText();
    if (text) {
      const input = document.getElementById("videoUrlInput");
      input.value = text.trim();
      handleUrlInputChange(input.value);
      showToast("Ссылка вставлена из буфера обмена", "info");
      fetchVideoInfo();
    }
  } catch (err) {
    showToast("Не удалось прочитать буфер. Вставьте вручную (Cmd+V / Ctrl+V)", "info");
  }
}

function detectPlatform(url) {
  const u = (url || "").toLowerCase();
  if (u.includes("youtube.com") || u.includes("youtu.be")) {
    return { name: "YouTube", cls: "youtube" };
  }
  if (u.includes("vk.com") || u.includes("vkvideo.ru")) {
    return { name: "VK Видео", cls: "vk" };
  }
  if (u.includes("rutube.ru")) {
    return { name: "RuTube", cls: "rutube" };
  }
  if (u.includes("tiktok.com")) {
    return { name: "TikTok", cls: "tiktok" };
  }
  if (u.includes("twitch.tv")) {
    return { name: "Twitch", cls: "twitch" };
  }
  if (u.includes("twitter.com") || u.includes("x.com")) {
    return { name: "X (Twitter)", cls: "x" };
  }
  if (u.includes("instagram.com")) {
    return { name: "Instagram", cls: "instagram" };
  }
  return null;
}

function handleUrlInputChange(val) {
  const badge = document.getElementById("platformBadge");
  const icon = document.getElementById("inputPrefixIcon");
  const btnClear = document.getElementById("btnClear");

  if (btnClear) {
    btnClear.style.display = (val && val.trim().length > 0) ? "flex" : "none";
  }

  const p = detectPlatform(val);
  if (p) {
    if (badge) {
      badge.className = `platform-badge ${p.cls}`;
      badge.innerText = p.name;
      badge.style.display = "inline-flex";
    }
    if (icon) icon.style.display = "none";
  } else {
    if (badge) badge.style.display = "none";
    if (icon) icon.style.display = "flex";
  }
}

function clearInput() {
  const input = document.getElementById("videoUrlInput");
  input.value = "";
  const btnClear = document.getElementById("btnClear");
  if (btnClear) btnClear.style.display = "none";
  const badge = document.getElementById("platformBadge");
  if (badge) badge.style.display = "none";
  const icon = document.getElementById("inputPrefixIcon");
  if (icon) icon.style.display = "flex";
  document.getElementById("videoConfigSection").style.display = "none";
  const playlistSec = document.getElementById("playlistConfigSection");
  if (playlistSec) playlistSec.style.display = "none";
  input.focus();
}

function setTestUrl(url) {
  const input = document.getElementById("videoUrlInput");
  input.value = url;
  handleUrlInputChange(url);
  document.getElementById("btnClear").style.display = "flex";
  fetchVideoInfo();
}

// Mode Selection (Video vs Audio)
function setMode(mode) {
  activeMode = mode;
  const btnVideo = document.getElementById("btnModeVideo");
  const btnAudio = document.getElementById("btnModeAudio");
  const videoPanel = document.getElementById("videoOptionsPanel");
  const audioPanel = document.getElementById("audioOptionsPanel");
  const btnDownloadText = document.getElementById("btnDownloadText");

  if (mode === "video") {
    btnVideo.classList.add("active");
    btnAudio.classList.remove("active");
    videoPanel.style.display = "grid";
    audioPanel.style.display = "none";
    btnDownloadText.innerText = "Скачать видео";
  } else {
    btnVideo.classList.remove("active");
    btnAudio.classList.add("active");
    videoPanel.style.display = "none";
    audioPanel.style.display = "grid";
    btnDownloadText.innerText = "Извлечь аудио (MP3)";
  }
}

// Accordion Toggles
function toggleAccordion(name) {
  const item = document.getElementById(name === "trim" ? "accordionTrim" : "accordionSubs");
  item.classList.toggle("open");
}

function updateSubsState() {
  const enabled = document.getElementById("checkEnableSubs").checked;
  const container = document.getElementById("subsOptionsContainer");
  const statusTag = document.getElementById("subsStatusTag");

  container.style.display = enabled ? "block" : "none";
  statusTag.innerText = enabled ? "Включено" : "Нет";
  statusTag.className = enabled ? "tag-status active" : "tag-status";
}

function setTrimFull() {
  document.getElementById("trimStart").value = "";
  document.getElementById("trimEnd").value = "";
  document.getElementById("trimStatusTag").innerText = "Выключено";
  document.getElementById("trimStatusTag").className = "tag-status";
}

document.getElementById("trimStart")?.addEventListener("input", checkTrimStatus);
document.getElementById("trimEnd")?.addEventListener("input", checkTrimStatus);

function checkTrimStatus() {
  const start = document.getElementById("trimStart").value.trim();
  const end = document.getElementById("trimEnd").value.trim();
  const statusTag = document.getElementById("trimStatusTag");
  if (start || end) {
    statusTag.innerText = `${start || '00:00'} - ${end || 'конец'}`;
    statusTag.className = "tag-status active";
  } else {
    statusTag.innerText = "Выключено";
    statusTag.className = "tag-status";
  }
}

// Fetch Video Info & Formats
async function fetchVideoInfo() {
  const urlInput = document.getElementById("videoUrlInput");
  const url = urlInput.value.trim();

  if (!url) {
    showToast("Пожалуйста, вставьте ссылку на YouTube видео или плейлист", "error");
    return;
  }

  const loadingState = document.getElementById("loadingState");
  const videoConfigSection = document.getElementById("videoConfigSection");
  const playlistConfigSection = document.getElementById("playlistConfigSection");

  loadingState.style.display = "block";
  videoConfigSection.style.display = "none";
  if (playlistConfigSection) playlistConfigSection.style.display = "none";

  try {
    const res = await fetch("/api/info", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url })
    });

    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || "Не удалось получить информацию о видео");
    }

    const data = await res.json();
    currentVideoInfo = data;

    if (data.is_playlist) {
      videoConfigSection.style.display = "none";
      renderPlaylistInfo(data);
      showToast(`Плейлист успешно загружен (${data.item_count || data.entries.length} видео)`, "success");
    } else {
      if (playlistConfigSection) playlistConfigSection.style.display = "none";
      renderVideoInfo(data);
      showToast("Информация о видео успешно загружена", "success");
    }
  } catch (err) {
    showToast(err.message, "error", 5000);
  } finally {
    loadingState.style.display = "none";
  }
}

// Playlist Batch Download System
let currentPlaylistData = null;
let selectedPlaylistIndices = new Set();
let currentPlaylistMode = "video";
const playlistTaskToIdxMap = new Map(); // taskId -> index in entries
const playlistIdxToTaskMap = new Map(); // index in entries -> task object

// Single & Photo active inline tasks
let currentSingleTaskId = null;
let currentPhotoTaskId = null;

function togglePlaylistMode(mode) {
  currentPlaylistMode = mode;
  const pillV = document.getElementById("pillPlaylistModeVideo");
  const pillA = document.getElementById("pillPlaylistModeAudio");
  const videoPanel = document.getElementById("playlistVideoOptionsPanel");
  const audioPanel = document.getElementById("playlistAudioOptionsPanel");
  if (pillV) pillV.classList.toggle("active", mode === "video");
  if (pillA) pillA.classList.toggle("active", mode === "audio");
  if (videoPanel) videoPanel.style.display = mode === "video" ? "grid" : "none";
  if (audioPanel) audioPanel.style.display = mode === "audio" ? "grid" : "none";
}

function renderPlaylistInfo(data) {
  currentPlaylistData = data;
  selectedPlaylistIndices.clear();
  playlistTaskToIdxMap.clear();
  playlistIdxToTaskMap.clear();
  const masterProg = document.getElementById("playlistMasterProgress");
  if (masterProg) masterProg.style.display = "none";
  (data.entries || []).forEach((_, idx) => selectedPlaylistIndices.add(idx));

  const playlistSec = document.getElementById("playlistConfigSection");
  const videoSec = document.getElementById("videoConfigSection");
  if (videoSec) videoSec.style.display = "none";

  const totalItems = data.item_count || (data.entries || []).length;
  const isCarousel = Boolean(data.is_carousel);
  const itemTypeLabel = isCarousel ? "слайдов" : "видео";
  const coverUrl = data.thumbnail || (data.entries && data.entries[0] && data.entries[0].thumbnail) || "";

  // Stacked Preview Banner Elements
  const coverImg = document.getElementById("playlistPreviewCover");
  const countOverlay = document.getElementById("playlistCoverCount");
  const bannerTitle = document.getElementById("playlistBannerTitleText");
  const bannerAuthor = document.getElementById("playlistAuthor");
  const bannerCount = document.getElementById("playlistBannerCountText");
  const platformName = document.getElementById("playlistPlatformName");
  const typeBadge = document.getElementById("playlistTypeBadge");

  if (coverImg) {
    if (coverUrl) {
      coverImg.src = coverUrl;
      coverImg.style.display = "block";
    } else {
      coverImg.style.display = "none";
    }
  }
  if (countOverlay) countOverlay.innerText = `${totalItems} ${itemTypeLabel}`;
  if (bannerTitle) bannerTitle.innerText = data.title || (isCarousel ? "Instagram Карусель" : "Плейлист");
  if (bannerAuthor) bannerAuthor.innerText = data.uploader || "Автор неизвестен";
  if (bannerCount) bannerCount.innerText = `${totalItems} ${itemTypeLabel}`;
  if (platformName) platformName.innerText = (data.platform && data.platform.name) || (isCarousel ? "Instagram" : "Плейлист");
  if (typeBadge) typeBadge.innerText = isCarousel ? "📸 КАРУСЕЛЬ" : "📁 ПЛЕЙЛИСТ";

  const plTitleEl = document.getElementById("playlistTitle");
  if (plTitleEl) plTitleEl.innerText = data.title || (isCarousel ? "Карусель" : "Плейлист");
  const plCountTag = document.getElementById("playlistCountTag");
  if (plCountTag) plCountTag.innerText = `${totalItems} ${itemTypeLabel}`;

  renderPlaylistItemsList();
  updateBatchDownloadButton();

  playlistSec.style.display = "block";
  playlistSec.scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderPlaylistItemsList() {
  const container = document.getElementById("playlistItemsList");
  if (!container || !currentPlaylistData) return;
  container.innerHTML = "";

  (currentPlaylistData.entries || []).forEach((item, idx) => {
    const isChecked = selectedPlaylistIndices.has(idx);
    const taskInfo = playlistIdxToTaskMap.get(idx);
    const isDownloading = taskInfo && ["downloading", "converting", "queued"].includes(taskInfo.status);
    const isCompleted = taskInfo && taskInfo.status === "completed";
    const isError = taskInfo && taskInfo.status === "error";

    const row = document.createElement("div");
    row.id = `playlistRow-${idx}`;
    let statusClass = "";
    if (isDownloading) statusClass = "is-downloading";
    else if (isCompleted) statusClass = "is-completed";
    else if (isError) statusClass = "is-error";

    row.className = `playlist-item-row ${isChecked ? 'selected' : ''} ${statusClass}`;
    row.onclick = (e) => {
      if (["BUTTON", "A", "INPUT", "SVG", "PATH", "LINE", "POLYGON"].includes(e.target.tagName)) return;
      if (e.target.closest("button") || e.target.closest("a")) return;
      if (!isDownloading) {
        togglePlaylistItem(idx);
      }
    };

    const cleanTitle = escapeHtml(item.title || (item.is_photo ? `Фото #${idx + 1}` : `Видео #${idx + 1}`));
    const durStr = item.is_photo ? "ФОТО" : (item.duration_str || "00:00");
    const thumb = item.thumbnail || item.photo_url || "/api/thumbnail/fallback";

    // Progress Column HTML
    let progressColHtml = "";
    let actionsHtml = "";
    const pct = taskInfo ? Math.min(100, Math.max(0, taskInfo.progress || 0)) : 0;

    if (taskInfo) {
      let statusLabel = "В очереди";
      let statusTypeClass = "queued";
      if (taskInfo.status === "downloading") {
        statusLabel = `⚡ ${pct.toFixed(1)}%`;
        statusTypeClass = "downloading";
      } else if (taskInfo.status === "converting") {
        statusLabel = "⚙ Сведение";
        statusTypeClass = "converting";
      } else if (taskInfo.status === "completed") {
        statusLabel = "✓ Скачано";
        statusTypeClass = "completed";
      } else if (taskInfo.status === "error") {
        statusLabel = "⚠ Ошибка";
        statusTypeClass = "error";
      }

      const speedStr = taskInfo.speed && !taskInfo.speed.includes("NA") ? taskInfo.speed : "";

      progressColHtml = `
        <div class="playlist-row-progress-col" id="plRowProgCol-${idx}">
          <div class="pl-prog-header">
            <span class="pl-prog-status ${statusTypeClass}" id="plRowStatus-${idx}">
              <span id="plRowStatusText-${idx}">${statusLabel}</span>
            </span>
            <span class="pl-prog-speed" id="plRowSpeed-${idx}">${speedStr}</span>
          </div>
          <div class="pl-prog-bar-track">
            <div class="pl-prog-bar-fill" id="plRowBarFill-${idx}" style="width: ${pct}%;"></div>
          </div>
        </div>
      `;

      if (isDownloading) {
        actionsHtml = `
          <div class="pl-prog-actions" id="plRowActions-${idx}">
            <button type="button" class="btn-pl-action cancel" title="Отменить скачивание" onclick="event.stopPropagation(); cancelTask('${taskInfo.taskId}')">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
            </button>
          </div>
        `;
      } else if (isCompleted && taskInfo.filename) {
        actionsHtml = `
          <div class="pl-prog-actions" id="plRowActions-${idx}">
            <button type="button" class="btn-pl-action play" title="Воспроизвести прямо сейчас" onclick="event.stopPropagation(); openPlayerModal('${encodeURIComponent(taskInfo.filename)}')">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>
            </button>
            <a href="/api/files/${encodeURIComponent(taskInfo.filename)}" download="${escapeHtml(taskInfo.filename)}" class="btn-pl-action" title="Сохранить на компьютер" onclick="event.stopPropagation();">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>
            </a>
          </div>
        `;
      }
    }

    const bgBarHtml = taskInfo ? `<div class="playlist-row-bg-bar" id="plRowBgBar-${idx}" style="width: ${pct}%;"></div>` : "";

    row.innerHTML = `
      ${bgBarHtml}
      <input type="checkbox" class="playlist-item-checkbox" ${isChecked ? 'checked' : ''} onchange="togglePlaylistItem(${idx})" ${isDownloading ? 'disabled' : ''} />
      <div class="playlist-item-thumb-box">
        <img src="${thumb}" alt="${cleanTitle}" loading="lazy" onerror="this.style.display='none'" />
        <span class="playlist-item-duration ${item.is_photo ? 'badge-photo' : ''}">${durStr}</span>
      </div>
      <div class="playlist-item-meta">
        <div class="playlist-item-title" title="${cleanTitle}">${cleanTitle}</div>
        <div class="playlist-item-channel">${escapeHtml(item.uploader || '')}</div>
      </div>
      ${progressColHtml}
      ${actionsHtml}
    `;
    container.appendChild(row);
  });
}

function updatePlaylistItemProgress(idx, task) {
  const existing = playlistIdxToTaskMap.get(idx) || {};
  playlistIdxToTaskMap.set(idx, {
    ...existing,
    taskId: task.id,
    status: task.status,
    progress: task.progress || 0,
    speed: task.speed || "--",
    eta: task.eta || "--",
    downloaded_str: task.downloaded_str || "0 B",
    filename: task.filename || existing.filename,
    error: task.error || existing.error
  });

  const row = document.getElementById(`playlistRow-${idx}`);
  if (!row) {
    return;
  }

  const pct = Math.min(100, Math.max(0, task.progress || 0));
  const isDownloading = ["downloading", "converting", "queued"].includes(task.status);
  const isCompleted = task.status === "completed";
  const isError = task.status === "error";

  row.classList.toggle("is-downloading", isDownloading);
  row.classList.toggle("is-completed", isCompleted);
  row.classList.toggle("is-error", isError);

  const chk = row.querySelector(".playlist-item-checkbox");
  if (chk) {
    chk.disabled = isDownloading;
  }

  let bgBar = document.getElementById(`plRowBgBar-${idx}`);
  if (!bgBar) {
    bgBar = document.createElement("div");
    bgBar.className = "playlist-row-bg-bar";
    bgBar.id = `plRowBgBar-${idx}`;
    row.prepend(bgBar);
  }
  bgBar.style.width = `${pct}%`;

  let statusLabel = "В очереди";
  let statusTypeClass = "queued";
  if (task.status === "downloading") {
    statusLabel = `⚡ ${pct.toFixed(1)}%`;
    statusTypeClass = "downloading";
  } else if (task.status === "converting") {
    statusLabel = "⚙ Сведение";
    statusTypeClass = "converting";
  } else if (task.status === "completed") {
    statusLabel = "✓ Скачано";
    statusTypeClass = "completed";
  } else if (task.status === "error") {
    statusLabel = "⚠ Ошибка";
    statusTypeClass = "error";
  }

  const speedStr = task.speed && !task.speed.includes("NA") ? task.speed : "";

  let progCol = document.getElementById(`plRowProgCol-${idx}`);
  if (!progCol) {
    progCol = document.createElement("div");
    progCol.className = "playlist-row-progress-col";
    progCol.id = `plRowProgCol-${idx}`;
    progCol.innerHTML = `
      <div class="pl-prog-header">
        <span class="pl-prog-status ${statusTypeClass}" id="plRowStatus-${idx}">
          <span id="plRowStatusText-${idx}">${statusLabel}</span>
        </span>
        <span class="pl-prog-speed" id="plRowSpeed-${idx}">${speedStr}</span>
      </div>
      <div class="pl-prog-bar-track">
        <div class="pl-prog-bar-fill" id="plRowBarFill-${idx}" style="width: ${pct}%;"></div>
      </div>
    `;
    const existingActions = document.getElementById(`plRowActions-${idx}`);
    if (existingActions) {
      row.insertBefore(progCol, existingActions);
    } else {
      row.appendChild(progCol);
    }
  } else {
    const statusEl = document.getElementById(`plRowStatus-${idx}`);
    const statusTextEl = document.getElementById(`plRowStatusText-${idx}`);
    const speedEl = document.getElementById(`plRowSpeed-${idx}`);
    const barFillEl = document.getElementById(`plRowBarFill-${idx}`);

    if (barFillEl) barFillEl.style.width = `${pct}%`;
    if (statusTextEl) statusTextEl.innerText = statusLabel;
    if (statusEl) statusEl.className = `pl-prog-status ${statusTypeClass}`;
    if (speedEl) speedEl.innerText = speedStr;
  }

  let actionsContainer = document.getElementById(`plRowActions-${idx}`);
  if (!actionsContainer) {
    actionsContainer = document.createElement("div");
    actionsContainer.className = "pl-prog-actions";
    actionsContainer.id = `plRowActions-${idx}`;
    row.appendChild(actionsContainer);
  }

  if (isDownloading) {
    actionsContainer.innerHTML = `
      <button type="button" class="btn-pl-action cancel" title="Отменить скачивание" onclick="event.stopPropagation(); cancelTask('${task.taskId || task.id}')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
      </button>
    `;
  } else if (isCompleted && (task.filename || existing.filename)) {
    const fn = task.filename || existing.filename;
    actionsContainer.innerHTML = `
      <button type="button" class="btn-pl-action play" title="Воспроизвести прямо сейчас" onclick="event.stopPropagation(); openPlayerModal('${encodeURIComponent(fn)}')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>
      </button>
      <a href="/api/files/${encodeURIComponent(fn)}" download="${escapeHtml(fn)}" class="btn-pl-action" title="Сохранить на компьютер" onclick="event.stopPropagation();">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>
      </a>
    `;
  } else if (isError) {
    actionsContainer.innerHTML = `
      <span class="pl-error-icon" title="${escapeHtml(task.error || 'Ошибка')}">⚠</span>
    `;
  }
}

function updateMasterPlaylistProgress() {
  const masterBanner = document.getElementById("playlistMasterProgress");
  if (!masterBanner) return;

  const total = playlistIdxToTaskMap.size;
  if (total === 0) {
    masterBanner.style.display = "none";
    return;
  }

  masterBanner.style.display = "block";

  let completedCount = 0;
  let activeCount = 0;
  let errorCount = 0;
  let sumProgress = 0;
  let activeSpeeds = [];

  playlistIdxToTaskMap.forEach((t) => {
    sumProgress += Math.min(100, Math.max(0, t.progress || 0));
    if (t.status === "completed") completedCount++;
    else if (["downloading", "converting", "queued"].includes(t.status)) {
      activeCount++;
      if (t.speed && !t.speed.includes("NA") && !t.speed.includes("--")) {
        activeSpeeds.push(t.speed);
      }
    } else if (["error", "cancelled"].includes(t.status)) {
      errorCount++;
    }
  });

  const avgPct = total > 0 ? (sumProgress / total) : 0;
  const fillEl = document.getElementById("masterProgressBarFill");
  const pctEl = document.getElementById("masterProgressPercent");
  const statsEl = document.getElementById("masterProgressStats");
  const speedEl = document.getElementById("masterProgressSpeed");
  const titleEl = document.getElementById("masterProgressTitle");
  const cancelBtn = document.getElementById("btnCancelMasterPlaylist");

  if (fillEl) fillEl.style.width = `${avgPct.toFixed(1)}%`;
  if (pctEl) pctEl.innerText = `${avgPct.toFixed(1)}%`;
  if (statsEl) statsEl.innerText = `${completedCount} из ${total} скачано`;

  if (activeSpeeds.length > 0) {
    if (speedEl) speedEl.innerText = activeSpeeds[0];
  } else if (completedCount === total) {
    if (speedEl) speedEl.innerText = "Готово";
  } else {
    if (speedEl) speedEl.innerText = "--";
  }

  if (completedCount === total && total > 0) {
    if (titleEl) titleEl.innerText = "✓ Плейлист полностью скачан!";
    if (cancelBtn) cancelBtn.style.display = "none";
  } else if (errorCount > 0 && activeCount === 0) {
    if (titleEl) titleEl.innerText = `Завершено с ошибками (${errorCount} сбоев)`;
    if (cancelBtn) cancelBtn.style.display = "none";
  } else {
    if (titleEl) titleEl.innerText = `Загрузка плейлиста (${activeCount} в процессе)...`;
    if (cancelBtn) cancelBtn.style.display = "inline-flex";
  }
}

function cancelActivePlaylistTasks() {
  let count = 0;
  playlistIdxToTaskMap.forEach((t) => {
    if (["queued", "downloading", "converting"].includes(t.status) && t.taskId) {
      cancelTask(t.taskId);
      count++;
    }
  });
  if (count > 0) {
    showToast(`Отменено ${count} задач плейлиста`, "info");
  }
}

function togglePlaylistItem(idx) {
  if (selectedPlaylistIndices.has(idx)) {
    selectedPlaylistIndices.delete(idx);
  } else {
    selectedPlaylistIndices.add(idx);
  }
  renderPlaylistItemsList();
  updateBatchDownloadButton();
}

function togglePlaylistSelectAll() {
  if (!currentPlaylistData) return;
  const total = (currentPlaylistData.entries || []).length;
  if (selectedPlaylistIndices.size === total) {
    selectedPlaylistIndices.clear();
  } else {
    currentPlaylistData.entries.forEach((_, idx) => selectedPlaylistIndices.add(idx));
  }
  renderPlaylistItemsList();
  updateBatchDownloadButton();
}

function updateBatchDownloadButton() {
  const count = selectedPlaylistIndices.size;
  const btnText = document.getElementById("btnBatchDownloadText");
  if (btnText) {
    btnText.innerText = `Скачать выбранные (${count})`;
  }
  const btn = document.getElementById("btnStartBatchDownload");
  if (btn) {
    btn.disabled = count === 0;
    btn.style.opacity = count === 0 ? "0.5" : "1";
  }
}

async function startBatchDownload() {
  if (!currentPlaylistData || selectedPlaylistIndices.size === 0) {
    showToast("Выберите хотя бы один элемент из списка", "error");
    return;
  }

  const items = [];
  const selectedIndicesInOrder = [];
  selectedPlaylistIndices.forEach(idx => {
    const item = currentPlaylistData.entries[idx];
    if (item) {
      items.push({
        url: item.url,
        title: item.title,
        index: idx + 1,
        is_photo: Boolean(item.is_photo),
        photo_url: item.photo_url || item.direct_url || (item.is_photo ? item.thumbnail : "")
      });
      selectedIndicesInOrder.push(idx);
    }
  });

  const playlistTitle = (currentPlaylistData?.title || "Плейлист").trim();
  const playlistThumbnail = currentPlaylistData?.thumbnail || (currentPlaylistData?.entries && currentPlaylistData.entries[0] && currentPlaylistData.entries[0].thumbnail) || "";
  const videoQuality = document.getElementById("selectPlaylistQuality")?.value || "best";
  const videoFormat = document.getElementById("selectPlaylistVideoFormat")?.value || "mp4";
  const audioFormat = document.getElementById("selectPlaylistAudioFormat")?.value || "mp3";
  const audioBitrate = document.getElementById("selectPlaylistAudioBitrate")?.value || "320";

  const payload = {
    items: items,
    mode: currentPlaylistMode,
    quality: videoQuality,
    video_format: videoFormat,
    audio_format: audioFormat,
    audio_quality: audioBitrate,
    embed_thumbnail: true,
    playlist_title: playlistTitle,
    playlist_thumbnail: playlistThumbnail
  };

  try {
    const res = await fetch("/api/download-batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });

    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || "Не удалось запустить пакетное скачивание");
    }

    const { queued_count, task_ids } = await res.json();
    showToast(`Добавлено в очередь: ${queued_count} элементов!`, "success");

    playlistTaskToIdxMap.clear();
    playlistIdxToTaskMap.clear();

    selectedIndicesInOrder.forEach((idx, i) => {
      const tid = task_ids[i];
      if (tid) {
        playlistTaskToIdxMap.set(tid, idx);
        playlistIdxToTaskMap.set(idx, {
          taskId: tid,
          status: "downloading",
          progress: 0,
          speed: "--",
          eta: "--",
          downloaded_str: "0 B",
          title: currentPlaylistData.entries[idx]?.title || "",
          filename: null
        });
      }
    });

    renderPlaylistItemsList();
    updateMasterPlaylistProgress();
    loadTasksQueue();
  } catch (err) {
    showToast(err.message, "error");
  }
}

// Chapter selection handler
function handleChapterSelect(chapterIndex) {
  if (!currentVideoInfo || !currentVideoInfo.chapters) return;
  const idx = parseInt(chapterIndex);
  if (isNaN(idx) || idx < 0 || idx >= currentVideoInfo.chapters.length) return;
  const ch = currentVideoInfo.chapters[idx];
  document.getElementById("trimStart").value = ch.start_str || "";
  document.getElementById("trimEnd").value = ch.end_str || "";
  checkTrimStatus();
  showToast(`Выбрана глава: «${ch.title}»`, "info");
}

// Render Video / Media Details to UI
function renderVideoInfo(data) {
  const videoConfigSection = document.getElementById("videoConfigSection");
  const photoCard = document.getElementById("photoPreviewCard");
  const videoCard = document.getElementById("videoPreviewCard");
  const configCard = document.getElementById("configCard");

  // Reset in-card progress for single video & photo
  resetSingleInlineProgress();
  const photoProg = document.getElementById("photoInlineProgress");
  if (photoProg) photoProg.style.display = "none";
  const btnPhoto = document.getElementById("btnDownloadPhoto");
  if (btnPhoto) btnPhoto.style.display = "inline-flex";

  // Handle Photo media (e.g. Instagram Photo)
  if (data.is_photo) {
    if (videoCard) videoCard.style.display = "none";
    if (configCard) configCard.style.display = "none";
    if (photoCard) {
      photoCard.style.display = "flex";
      const photoImg = document.getElementById("photoPreviewImg");
      if (photoImg) photoImg.src = data.photo_url || data.thumbnail || "";
      const pTitle = document.getElementById("photoTitle");
      if (pTitle) pTitle.innerText = data.title || "Instagram Photo";
      const pAuthor = document.getElementById("photoAuthor");
      if (pAuthor) pAuthor.innerText = data.uploader || "Instagram User";
      const pDesc = document.getElementById("photoDescription");
      if (pDesc) pDesc.innerText = data.description || "Без описания";
      const pOrig = document.getElementById("photoOriginalLink");
      if (pOrig) pOrig.href = data.original_url || "#";
      const pRes = document.getElementById("photoResolutionBadge");
      const pTag = document.getElementById("photoResolutionTag");
      const resLabel = (data.resolutions && data.resolutions[0] && data.resolutions[0].label) || "HQ ФОТО";
      if (pRes) pRes.innerText = "HQ ФОТО";
      if (pTag) pTag.innerText = resLabel;
    }
    videoConfigSection.style.display = "flex";
    videoConfigSection.scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }

  // Not a photo: show standard video card and configuration panel
  if (photoCard) photoCard.style.display = "none";
  if (videoCard) videoCard.style.display = "grid";
  if (configCard) configCard.style.display = "block";

  document.getElementById("videoThumb").src = data.thumbnail || "";
  document.getElementById("videoTitle").innerText = data.title || "YouTube Video";
  document.getElementById("authorName").innerText = data.uploader || "Автор неизвестен";
  document.getElementById("viewsCount").innerText = `${(data.view_count || 0).toLocaleString()} просмотров`;
  document.getElementById("videoDuration").innerText = data.duration_str || "00:00";
  document.getElementById("videoDescription").innerText = data.description || "Без описания";

  const origLink = document.getElementById("videoOriginalLink");
  origLink.href = data.original_url || "#";

  // Populate Quality Select options
  const selectQuality = document.getElementById("selectQuality");
  selectQuality.innerHTML = '<option value="best" selected>🌟 Максимальное доступное (Auto Best)</option>';

  if (data.resolutions && data.resolutions.length > 0) {
    data.resolutions.forEach(res => {
      const opt = document.createElement("option");
      opt.value = res.height;
      opt.innerText = res.label;
      selectQuality.appendChild(opt);
    });
  }

  // Populate Chapters Dropdown
  const chaptersContainer = document.getElementById("chaptersDropdownContainer");
  const selectChapter = document.getElementById("selectChapter");
  if (chaptersContainer && selectChapter) {
    if (data.chapters && data.chapters.length > 0) {
      selectChapter.innerHTML = '<option value="">Выберите главу для авто-подстановки таймкода...</option>';
      data.chapters.forEach((ch, idx) => {
        const opt = document.createElement("option");
        opt.value = idx;
        opt.innerText = `${ch.start_str} - ${ch.end_str}: ${ch.title}`;
        selectChapter.appendChild(opt);
      });
      chaptersContainer.style.display = "block";
    } else {
      chaptersContainer.style.display = "none";
    }
  }

  // Populate Subtitle Languages
  const selectSubLang = document.getElementById("selectSubLang");
  selectSubLang.innerHTML = "";
  if (data.subtitles && data.subtitles.length > 0) {
    data.subtitles.forEach(sub => {
      const opt = document.createElement("option");
      opt.value = sub.code;
      opt.innerText = sub.name;
      if (sub.code === "ru") opt.selected = true;
      selectSubLang.appendChild(opt);
    });
  } else {
    selectSubLang.innerHTML = '<option value="ru">Русский (ru)</option><option value="en">English (en)</option>';
  }

  // Pre-fill end trim time with video duration for convenience
  if (data.duration_str) {
    document.getElementById("trimEnd").placeholder = data.duration_str;
  }

  videoConfigSection.style.display = "flex";
  videoConfigSection.scrollIntoView({ behavior: "smooth", block: "start" });
}

// Start Photo Download
async function startPhotoDownload() {
  if (!currentVideoInfo || !currentVideoInfo.is_photo) return;
  const btn = document.getElementById("btnDownloadPhoto");
  if (btn) {
    btn.disabled = true;
    btn.classList.add("loading");
  }

  try {
    const payload = {
      url: currentVideoInfo.original_url || currentVideoInfo.photo_url,
      mode: "photo",
      is_photo: true,
      photo_url: currentVideoInfo.photo_url || currentVideoInfo.thumbnail,
      title: currentVideoInfo.title || "Instagram Photo"
    };

    const res = await fetch("/api/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });

    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || "Не удалось запустить скачивание фото");
    }

    const { task_id } = await res.json();
    currentPhotoTaskId = task_id;
    showToast("Скачивание фото началось!", "success");

    if (btn) btn.style.display = "none";
    const prog = document.getElementById("photoInlineProgress");
    if (prog) prog.style.display = "block";
    const bar = document.getElementById("photoProgBarFill");
    if (bar) bar.style.width = "0%";
    const stat = document.getElementById("photoProgStatusText");
    if (stat) stat.innerText = "Скачивание фото...";
    const comp = document.getElementById("photoCompletedActions");
    if (comp) comp.style.display = "none";

    loadTasksQueue();
  } catch (err) {
    showToast(err.message, "error");
    if (btn) btn.style.display = "inline-flex";
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.classList.remove("loading");
    }
  }
}

// Downloads Queue Management (Multi-task Support)
const activeTasksMap = new Map(); // taskId -> { eventSource }

function escapeHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

// Load existing/active tasks on startup
async function loadTasksQueue() {
  try {
    const res = await fetch("/api/tasks");
    if (!res.ok) return;
    const data = await res.json();
    const tasks = data.tasks || [];
    if (tasks.length === 0) return;

    for (const task of tasks) {
      handleTaskUpdate(task);
    }
    updateQueueBadge();
  } catch (err) {
    console.warn("Could not load tasks queue", err);
  }
}

// Start Download
async function startDownloadProcess() {
  if (!currentVideoInfo) {
    showToast("Сначала выберите медиа для скачивания", "error");
    return;
  }

  if (currentVideoInfo.is_photo) {
    return startPhotoDownload();
  }

  const url = document.getElementById("videoUrlInput").value.trim();
  const quality = document.getElementById("selectQuality").value;
  const videoFormat = document.getElementById("selectVideoFormat").value;
  const audioFormat = document.getElementById("selectAudioFormat").value;
  const audioBitrate = document.getElementById("selectAudioBitrate").value;

  const trimStart = document.getElementById("trimStart").value.trim() || null;
  const trimEnd = document.getElementById("trimEnd").value.trim() || null;

  const downloadSubs = document.getElementById("checkEnableSubs").checked;
  const subLang = document.getElementById("selectSubLang").value;
  const embedSubs = document.getElementById("checkEmbedSubs").checked;
  const embedThumb = document.getElementById("checkEmbedThumb").checked;
  const splitChapters = document.getElementById("checkSplitChapters") ? document.getElementById("checkSplitChapters").checked : false;

  const payload = {
    url: url,
    title: currentVideoInfo.title,
    mode: activeMode,
    quality: quality,
    video_format: videoFormat,
    audio_format: audioFormat,
    audio_quality: audioBitrate,
    trim_start: trimStart,
    trim_end: trimEnd,
    download_subtitles: downloadSubs,
    subtitles_lang: subLang,
    embed_subtitles: embedSubs,
    embed_thumbnail: embedThumb,
    split_chapters: splitChapters,
  };

  try {
    const res = await fetch("/api/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });

    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || "Не удалось запустить скачивание");
    }

    const { task_id } = await res.json();
    currentSingleTaskId = task_id;

    let formatTag = "";
    if (activeMode === "video") {
      if (videoFormat === "gif") {
        formatTag = "GIF Анимация";
      } else {
        formatTag = `${!isNaN(quality) ? quality + "p" : (quality === "best" ? "Best" : quality.toUpperCase())} ${videoFormat.toUpperCase()}`;
      }
    } else {
      formatTag = `${audioBitrate}kbps ${audioFormat.toUpperCase()}`;
    }

    // Activate In-Card Progress Deck
    const btnDownload = document.getElementById("btnStartDownload");
    if (btnDownload) btnDownload.style.display = "none";

    const singleProg = document.getElementById("singleInlineProgress");
    if (singleProg) singleProg.style.display = "block";

    const formatTagEl = document.getElementById("singleProgFormatTag");
    if (formatTagEl) formatTagEl.innerText = formatTag;

    const statusPill = document.getElementById("singleProgStatusPill");
    if (statusPill) {
      statusPill.innerHTML = `<span class="pulse-dot-cyan"></span><span id="singleProgStatusText">Инициализация загрузки...</span>`;
    }

    const fillEl = document.getElementById("singleProgBarFill");
    if (fillEl) fillEl.style.width = "0%";

    const pctEl = document.getElementById("singleProgPct");
    if (pctEl) pctEl.innerText = "0%";

    const dlEl = document.getElementById("singleProgDownloaded");
    if (dlEl) dlEl.innerText = "0 B";

    const speedEl = document.getElementById("singleProgSpeed");
    if (speedEl) speedEl.innerText = "--";

    const etaEl = document.getElementById("singleProgEta");
    if (etaEl) etaEl.innerText = "--";

    const completedActions = document.getElementById("singleProgCompletedActions");
    if (completedActions) completedActions.style.display = "none";

    const cancelBtn = document.getElementById("btnSingleCancel");
    if (cancelBtn) cancelBtn.style.display = "inline-flex";

    const initialTask = {
      id: task_id,
      title: currentVideoInfo.title || "YouTube Video",
      format_tag: formatTag,
      status: "downloading",
      progress: 0,
      downloaded_str: "0 B",
      speed: "--",
      eta: "--",
      filename: null,
      error: null,
      logs: ["Инициализация загрузки..."]
    };

    handleTaskUpdate(initialTask);
    showToast("Скачивание началось!", "info");
  } catch (err) {
    showToast(err.message, "error");
  }
}

// Global Progress Stream & Task Coordination
let globalProgressEventSource = null;
let globalProgressPollTimer = null;
const terminalNotifiedTasks = new Set();
let historyDebounceTimer = null;

function scheduleHistoryReload() {
  if (historyDebounceTimer) clearTimeout(historyDebounceTimer);
  historyDebounceTimer = setTimeout(() => {
    loadHistoryFiles(true);
  }, 800);
}

function handleTaskUpdate(task) {
  if (!task || !task.id) return;

  activeTasksMap.set(task.id, task);
  renderOrUpdateTaskCard(task);
  updateQueueBadge();

  // In-line Playlist Row & Master Progress Update
  if (playlistTaskToIdxMap.has(task.id)) {
    const idx = playlistTaskToIdxMap.get(task.id);
    updatePlaylistItemProgress(idx, task);
    updateMasterPlaylistProgress();
  }

  // In-Card Single Video Progress Update
  if (currentSingleTaskId === task.id) {
    updateSingleInlineProgress(task);
  }

  // In-Card Photo Progress Update
  if (currentPhotoTaskId === task.id) {
    updatePhotoInlineProgress(task);
  }

  // Live update Task Logs Modal if open
  if (currentModalTaskId === task.id) {
    const pre = document.getElementById("taskLogsPre");
    if (pre && task.logs && task.logs.length > 0) {
      const atBottom = pre.scrollHeight - pre.scrollTop <= pre.clientHeight + 60;
      pre.innerText = task.logs.join("\n");
      if (atBottom) {
        pre.scrollTop = pre.scrollHeight;
      }
    }
  }

  // Terminal status notification (toast once per task)
  if (["completed", "error"].includes(task.status) && !terminalNotifiedTasks.has(task.id)) {
    terminalNotifiedTasks.add(task.id);
    if (task.status === "completed") {
      showToast(`«${task.title || 'Видео'}» успешно скачано!`, "success");
      scheduleHistoryReload();
    } else if (task.status === "error") {
      showToast(`Ошибка: ${task.error || 'Сбой скачивания'}`, "error");
    }
  }
}

function initGlobalProgressStream() {
  if (globalProgressEventSource) {
    try { globalProgressEventSource.close(); } catch (_) {}
    globalProgressEventSource = null;
  }

  try {
    globalProgressEventSource = new EventSource("/api/progress-stream");

    globalProgressEventSource.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data && Array.isArray(data.tasks)) {
          for (const t of data.tasks) {
            handleTaskUpdate(t);
          }
        }
      } catch (e) {
        console.error("Global progress stream parse error", e);
      }
    };

    globalProgressEventSource.onerror = () => {
      // If SSE stream encounters an error or reconnects, fallback to polling
      try { globalProgressEventSource.close(); } catch (_) {}
      globalProgressEventSource = null;
      startProgressPollingFallback();
    };
  } catch (e) {
    startProgressPollingFallback();
  }
}

function startProgressPollingFallback() {
  if (globalProgressPollTimer) return;
  globalProgressPollTimer = setInterval(async () => {
    try {
      const res = await fetch("/api/tasks");
      if (!res.ok) return;
      const data = await res.json();
      if (data && Array.isArray(data.tasks)) {
        for (const t of data.tasks) {
          handleTaskUpdate(t);
        }
      }
    } catch (_) {}
  }, 1000);
}

// Backward-compatibility stub
function subscribeToTask(taskId) {
  loadTasksQueue();
}

function updateSingleInlineProgress(task) {
  const singleProg = document.getElementById("singleInlineProgress");
  if (!singleProg) return;

  const pct = Math.min(100, Math.max(0, task.progress || 0));
  const fillEl = document.getElementById("singleProgBarFill");
  if (fillEl) fillEl.style.width = `${pct}%`;

  const pctEl = document.getElementById("singleProgPct");
  if (pctEl) pctEl.innerText = `${pct.toFixed(1)}%`;

  const downloadedStr = task.downloaded_str && !task.downloaded_str.includes("NA") && !task.downloaded_str.includes("N/A")
    ? task.downloaded_str
    : (task.status === "completed" ? "Готово" : "0 B");
  const dlEl = document.getElementById("singleProgDownloaded");
  if (dlEl) dlEl.innerText = downloadedStr;

  const speedStr = task.speed && !task.speed.includes("NA") ? task.speed : "--";
  const speedEl = document.getElementById("singleProgSpeed");
  if (speedEl) speedEl.innerText = speedStr;

  const etaStr = task.eta && !task.eta.includes("NA") ? task.eta : "--";
  const etaEl = document.getElementById("singleProgEta");
  if (etaEl) etaEl.innerText = etaStr;

  const statusPill = document.getElementById("singleProgStatusPill");
  if (statusPill) {
    if (task.status === "downloading") {
      statusPill.innerHTML = `<span class="pulse-dot-cyan"></span><span id="singleProgStatusText">Скачивание (${pct.toFixed(1)}%)</span>`;
    } else if (task.status === "converting") {
      statusPill.innerHTML = `<span class="pulse-dot-cyan"></span><span id="singleProgStatusText">Сведение потоков FFmpeg...</span>`;
    } else if (task.status === "completed") {
      statusPill.innerHTML = `<span style="color: #10b981; font-weight: 700;">✓ Скачивание завершено!</span>`;
    } else if (task.status === "error") {
      statusPill.innerHTML = `<span style="color: #f87171; font-weight: 700;">⚠ Ошибка скачивания</span>`;
    } else if (task.status === "cancelled") {
      statusPill.innerHTML = `<span style="color: #9ca3af;">✕ Отменено</span>`;
    }
  }

  if (task.status === "completed" && task.filename) {
    const cancelBtn = document.getElementById("btnSingleCancel");
    if (cancelBtn) cancelBtn.style.display = "none";

    const completedActions = document.getElementById("singleProgCompletedActions");
    if (completedActions) {
      completedActions.style.display = "block";
    }

    const btnPlay = document.getElementById("btnSingleInlinePlay");
    if (btnPlay) {
      btnPlay.onclick = () => openPlayerModal(task.filename);
    }

    const btnSave = document.getElementById("btnSingleInlineSave");
    if (btnSave) {
      btnSave.href = `/api/files/${encodeURIComponent(task.filename)}`;
      btnSave.download = task.filename;
    }
  } else if (task.status === "error" || task.status === "cancelled") {
    const cancelBtn = document.getElementById("btnSingleCancel");
    if (cancelBtn) cancelBtn.style.display = "none";
  }
}

function cancelCurrentSingleTask() {
  if (currentSingleTaskId) {
    cancelTask(currentSingleTaskId);
  }
  resetSingleInlineProgress();
}

function resetSingleInlineProgress() {
  const singleProg = document.getElementById("singleInlineProgress");
  if (singleProg) singleProg.style.display = "none";

  const btnDownload = document.getElementById("btnStartDownload");
  if (btnDownload) btnDownload.style.display = "inline-flex";

  currentSingleTaskId = null;
}

function updatePhotoInlineProgress(task) {
  const photoProg = document.getElementById("photoInlineProgress");
  if (!photoProg) return;

  const pct = Math.min(100, Math.max(0, task.progress || 0));
  const fillEl = document.getElementById("photoProgBarFill");
  if (fillEl) fillEl.style.width = `${pct}%`;

  const statusText = document.getElementById("photoProgStatusText");
  if (statusText) {
    if (task.status === "downloading") {
      statusText.innerText = `Скачивание фото (${pct.toFixed(0)}%)...`;
    } else if (task.status === "completed") {
      statusText.innerText = "✓ Фото успешно сохранено!";
    } else if (task.status === "error") {
      statusText.innerText = "⚠ Ошибка сохранения фото";
    }
  }

  if (task.status === "completed" && task.filename) {
    const completedActions = document.getElementById("photoCompletedActions");
    if (completedActions) completedActions.style.display = "block";

    const btnOpen = document.getElementById("btnPhotoInlineOpen");
    if (btnOpen) {
      btnOpen.onclick = () => {
        openLightboxModal({
          filename: task.filename,
          url: `/api/stream/${encodeURIComponent(task.filename)}`,
          title: currentVideoInfo?.title || task.filename,
          uploader: currentVideoInfo?.uploader || "Instagram"
        });
      };
    }

    const btnSave = document.getElementById("btnPhotoInlineSave");
    if (btnSave) {
      btnSave.href = `/api/files/${encodeURIComponent(task.filename)}`;
      btnSave.download = task.filename;
    }
  }
}

// Floating Downloads Drawer Controls
function openDownloadsDrawer() {
  const drawer = document.getElementById("downloadsDrawer");
  const overlay = document.getElementById("downloadsDrawerOverlay");
  const btn = document.getElementById("btnDownloadsDrawer");
  if (drawer) drawer.classList.add("open");
  if (overlay) overlay.classList.add("active");
  if (btn) btn.classList.add("active");
}

function closeDownloadsDrawer() {
  const drawer = document.getElementById("downloadsDrawer");
  const overlay = document.getElementById("downloadsDrawerOverlay");
  const btn = document.getElementById("btnDownloadsDrawer");
  if (drawer) drawer.classList.remove("open");
  if (overlay) overlay.classList.remove("active");
  if (btn) btn.classList.remove("active");
}

function toggleDownloadsDrawer() {
  const drawer = document.getElementById("downloadsDrawer");
  if (drawer && drawer.classList.contains("open")) {
    closeDownloadsDrawer();
  } else {
    openDownloadsDrawer();
  }
}

// Global hotkeys: Esc closes drawer / logs modal; D toggles drawer when not typing in inputs
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    closeDownloadsDrawer();
    closeTaskLogsModal();
  } else if ((e.key === "d" || e.key === "D" || e.key === "в" || e.key === "В") && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) {
    toggleDownloadsDrawer();
  }
});

// Render or update compact individual Task Row in Floating Drawer
function renderOrUpdateTaskCard(task) {
  const queueList = document.getElementById("queueList");
  if (!queueList) return;

  let row = document.getElementById(`taskCard-${task.id}`);
  if (!row) {
    row = document.createElement("div");
    row.id = `taskCard-${task.id}`;
    queueList.prepend(row);
  }

  row.className = `drawer-task-row status-${task.status}`;

  const pct = Math.min(100, Math.max(0, task.progress || 0));
  const isDownloading = ["downloading", "converting", "queued"].includes(task.status);

  // Status pill
  let statusText = "В очереди";
  let statusClass = "queued";
  if (task.status === "downloading") {
    statusText = `⚡ ${pct.toFixed(0)}%`;
    statusClass = "downloading";
  } else if (task.status === "converting") {
    statusText = "⚙ Сведение";
    statusClass = "converting";
  } else if (task.status === "completed") {
    statusText = "✓ Готово";
    statusClass = "completed";
  } else if (task.status === "error") {
    statusText = "⚠ Ошибка";
    statusClass = "error";
  } else if (task.status === "cancelled") {
    statusText = "✕ Отменено";
    statusClass = "cancelled";
  }
  const statusPillHtml = `<span class="drawer-status-pill ${statusClass}">${statusText}</span>`;

  // Format Tag
  const formatTagHtml = task.format_tag
    ? `<span class="drawer-format-tag">${escapeHtml(task.format_tag)}</span>`
    : "";

  // Thumbnail / icon
  let thumbHtml = "";
  if (task.filename) {
    thumbHtml = `<img src="/api/thumbnail/${encodeURIComponent(task.filename)}" onerror="this.outerHTML='<span class=\\'drawer-row-thumb-icon\\'>MP4</span>'" loading="lazy" />`;
  } else {
    const rawTag = task.format_tag ? escapeHtml(task.format_tag.split(" ")[0]) : 'YT';
    thumbHtml = `<span class="drawer-row-thumb-icon">${rawTag}</span>`;
  }

  // Telemetry string
  const speedStr = task.speed && !task.speed.includes("NA") ? task.speed : "";
  const downloadedStr = task.downloaded_str && !task.downloaded_str.includes("NA") && !task.downloaded_str.includes("N/A")
    ? task.downloaded_str
    : (task.status === "completed" ? "Готово" : "0 B");
  const etaStr = task.eta && !task.eta.includes("NA") && !task.eta.includes("--") ? `ETA ${task.eta}` : "";

  // Right Actions
  let actionsHtml = "";
  if (isDownloading) {
    actionsHtml = `
      <button type="button" class="btn-drawer-action cancel" onclick="cancelTask('${task.id}')" title="Отменить загрузку">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
      </button>
      <button type="button" class="btn-drawer-action logs" onclick="openTaskLogsModal('${task.id}')" title="Системный лог yt-dlp">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="4 17 10 11 4 5"></polyline><line x1="12" y1="19" x2="20" y2="19"></line></svg>
      </button>
    `;
  } else if (task.status === "completed" && task.filename) {
    const encodedFn = encodeURIComponent(task.filename);
    actionsHtml = `
      <button type="button" class="btn-drawer-action play" onclick="openPlayerModal('${encodedFn}')" title="Воспроизвести прямо сейчас">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>
      </button>
      <a href="/api/files/${encodedFn}" download="${escapeHtml(task.filename)}" class="btn-drawer-action download" title="Сохранить файл на диск">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>
      </a>
      <button type="button" class="btn-drawer-action logs" onclick="openTaskLogsModal('${task.id}')" title="Системный лог yt-dlp">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="4 17 10 11 4 5"></polyline><line x1="12" y1="19" x2="20" y2="19"></line></svg>
      </button>
      <button type="button" class="btn-drawer-action remove" onclick="removeTaskCard('${task.id}')" title="Убрать из списка">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
      </button>
    `;
  } else if (task.status === "error") {
    actionsHtml = `
      <button type="button" class="btn-drawer-action logs" onclick="openTaskLogsModal('${task.id}')" title="Посмотреть текст ошибки">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="8" x2="12" y2="12"></line><line x1="12" y1="16" x2="12.01" y2="16"></line></svg>
      </button>
      <button type="button" class="btn-drawer-action remove" onclick="removeTaskCard('${task.id}')" title="Убрать из списка">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
      </button>
    `;
  } else {
    actionsHtml = `
      <button type="button" class="btn-drawer-action remove" onclick="removeTaskCard('${task.id}')" title="Убрать из списка">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
      </button>
    `;
  }

  const cleanTitle = escapeHtml(task.title || 'Медиа файл');

  row.innerHTML = `
    <div class="drawer-row-left">
      <div class="drawer-row-thumb">
        ${thumbHtml}
      </div>
      <div class="drawer-row-content">
        <div class="drawer-row-header">
          <span class="drawer-row-title" title="${cleanTitle}">${cleanTitle}</span>
          <div class="drawer-row-badges">
            ${formatTagHtml}
            ${statusPillHtml}
          </div>
        </div>
        <div class="drawer-row-progress-track">
          <div class="drawer-row-progress-fill" style="width: ${pct}%;"></div>
        </div>
        <div class="drawer-row-telemetry">
          ${speedStr ? `<span class="drawer-telemetry-speed">${speedStr}</span><span class="drawer-telemetry-sep">&bull;</span>` : ''}
          <span class="drawer-telemetry-pct">${pct.toFixed(0)}%</span>
          <span class="drawer-telemetry-sep">&bull;</span>
          <span class="drawer-telemetry-size">${downloadedStr}</span>
          ${etaStr ? `<span class="drawer-telemetry-sep">&bull;</span><span class="drawer-telemetry-eta">${etaStr}</span>` : ''}
        </div>
      </div>
    </div>
    <div class="drawer-row-actions">
      ${actionsHtml}
    </div>
  `;
}

// Update Queue Badge and Drawer state
function updateQueueBadge() {
  const badge = document.getElementById("drawerQueueBadge");
  const pulseDot = document.getElementById("drawerPulseDot");
  const subtitle = document.getElementById("drawerStatusSubtitle");
  const emptyState = document.getElementById("drawerEmptyState");
  const clearBtn = document.getElementById("btnClearFinishedQueue");
  const rows = document.querySelectorAll(".drawer-task-row");

  const totalCount = rows.length;
  if (emptyState) {
    emptyState.style.display = totalCount === 0 ? "flex" : "none";
  }

  const activeRows = document.querySelectorAll(
    ".drawer-task-row.status-downloading, .drawer-task-row.status-converting, .drawer-task-row.status-queued"
  );
  const activeCount = activeRows.length;

  if (pulseDot) {
    pulseDot.style.display = activeCount > 0 ? "block" : "none";
  }

  if (badge) {
    if (activeCount > 0) {
      badge.style.display = "inline-flex";
      badge.innerText = activeCount;
      badge.style.background = "rgba(255, 42, 84, 0.25)";
      badge.style.color = "#ff5277";
    } else if (totalCount > 0) {
      badge.style.display = "inline-flex";
      badge.innerText = totalCount;
      badge.style.background = "rgba(255, 255, 255, 0.08)";
      badge.style.color = "var(--text-muted)";
    } else {
      badge.style.display = "none";
    }
  }

  if (subtitle) {
    if (activeCount > 0) {
      subtitle.innerText = `${activeCount} активн${activeCount === 1 ? 'ая' : 'ых'} загрузк${activeCount === 1 ? 'а' : 'и'}`;
      subtitle.style.color = "#ff5277";
    } else if (totalCount > 0) {
      subtitle.innerText = `Все завершены (${totalCount})`;
      subtitle.style.color = "var(--green-accent)";
    } else {
      subtitle.innerText = "Очередь пуста";
      subtitle.style.color = "var(--text-muted)";
    }
  }

  if (clearBtn) {
    const finishedCount = document.querySelectorAll(
      ".drawer-task-row.status-completed, .drawer-task-row.status-error, .drawer-task-row.status-cancelled"
    ).length;
    clearBtn.style.display = finishedCount > 0 ? "inline-flex" : "none";
  }
}

// Cancel Active Task by ID
async function cancelTask(taskId) {
  try {
    const res = await fetch(`/api/cancel/${taskId}`, { method: "POST" });
    if (res.ok) {
      showToast("Задача отменена", "info");
      const item = activeTasksMap.get(taskId);
      if (item && item.eventSource) {
        item.eventSource.close();
      }
    } else {
      showToast("Не удалось отменить задачу", "error");
    }
  } catch (err) {
    showToast("Ошибка при отмене: " + err.message, "error");
  }
}

// Remove Task Row from UI
function removeTaskCard(taskId) {
  const row = document.getElementById(`taskCard-${taskId}`);
  if (row) {
    row.remove();
  }
  if (activeTasksMap.has(taskId)) {
    const item = activeTasksMap.get(taskId);
    if (item && item.eventSource) {
      item.eventSource.close();
    }
    activeTasksMap.delete(taskId);
  }
  updateQueueBadge();
}

// Clear all finished tasks (completed, error, cancelled)
async function clearFinishedTasks() {
  try {
    await fetch("/api/tasks/clear-finished", { method: "POST" });
  } catch (e) {}

  const finishedRows = document.querySelectorAll(
    ".drawer-task-row.status-completed, .drawer-task-row.status-error, .drawer-task-row.status-cancelled"
  );
  finishedRows.forEach(row => row.remove());
  updateQueueBadge();
  showToast("Завершённые задачи очищены", "info");
}

// Task Logs Modal Controls
let currentModalTaskId = null;

function openTaskLogsModal(taskId) {
  currentModalTaskId = taskId;
  const modal = document.getElementById("taskLogsModal");
  const title = document.getElementById("logsModalTitle");
  const sub = document.getElementById("logsModalSub");
  const pre = document.getElementById("taskLogsPre");
  if (!modal || !pre) return;

  const task = activeTasksMap.get(taskId);
  const taskTitle = task?.title ? `«${task.title}»` : taskId;
  if (title) title.innerText = "Системный лог процесса";
  if (sub) sub.innerText = `Задача: ${taskTitle}`;

  const logs = task && task.logs && task.logs.length > 0 ? task.logs.join("\n") : "Лог пуст или задача ещё инициализируется...";
  pre.innerText = logs;

  modal.style.display = "flex";
  setTimeout(() => {
    pre.scrollTop = pre.scrollHeight;
  }, 40);
}

function closeTaskLogsModal() {
  const modal = document.getElementById("taskLogsModal");
  if (modal) modal.style.display = "none";
  currentModalTaskId = null;
}

function copyTaskLogs() {
  const pre = document.getElementById("taskLogsPre");
  if (!pre) return;
  navigator.clipboard.writeText(pre.innerText).then(() => {
    showToast("Лог скопирован в буфер обмена", "success");
  }).catch(() => {
    showToast("Не удалось скопировать лог", "error");
  });
}

// History State
let allHistoryFiles = [];
let allPlaylists = [];
let activePlaylistFolder = null;
let currentPlaylistQueue = [];
let currentPlaylistQueueIndex = -1;
let historyFilter = "all"; // "all", "playlists", "video", "audio"
let historySearchQuery = "";
let historyViewMode = localStorage.getItem("yt_history_view") || "grid";

// Set initial view mode icon
document.addEventListener("DOMContentLoaded", () => {
  const gridBtn = document.getElementById("btnViewGrid");
  const listBtn = document.getElementById("btnViewList");
  const container = document.getElementById("filesList");
  if (gridBtn && listBtn && container) {
    if (historyViewMode === "list") {
      gridBtn.classList.remove("active");
      listBtn.classList.add("active");
      container.classList.add("list-view");
    } else {
      gridBtn.classList.add("active");
      listBtn.classList.remove("active");
      container.classList.remove("list-view");
    }
  }
});

// Load History Files
async function loadHistoryFiles(silent = false) {
  const loading = document.getElementById("historyLoading");
  const empty = document.getElementById("historyEmpty");
  const list = document.getElementById("filesList");
  const badge = document.getElementById("filesCountBadge");

  if (!silent) {
    loading.style.display = "flex";
    empty.style.display = "none";
    list.innerHTML = "";
  }

  try {
    const res = await fetch("/api/history");
    const data = await res.json();
    if (data.user) {
      currentUser = data.user;
      updateProfileUI();
    }
    allHistoryFiles = data.files || [];
    allPlaylists = data.playlists || [];

    // Update pill counters
    const totalCount = allHistoryFiles.length;
    const playlistCount = allPlaylists.length;
    const videoCount = allHistoryFiles.filter(f => f.is_video).length;
    const audioCount = allHistoryFiles.filter(f => f.is_audio).length;
    const photoCount = allHistoryFiles.filter(f => f.is_photo || f.is_image).length;

    badge.innerText = totalCount;
    const countAllEl = document.getElementById("countAll");
    const countPlaylistsEl = document.getElementById("countPlaylists");
    const countVideosEl = document.getElementById("countVideos");
    const countAudioEl = document.getElementById("countAudio");
    const countPhotosEl = document.getElementById("countPhotos");
    if (countAllEl) countAllEl.innerText = totalCount;
    if (countPlaylistsEl) countPlaylistsEl.innerText = playlistCount;
    if (countVideosEl) countVideosEl.innerText = videoCount;
    if (countAudioEl) countAudioEl.innerText = audioCount;
    if (countPhotosEl) countPhotosEl.innerText = photoCount;

    // Render cards
    renderHistoryCards();

    if (!silent) {
      loading.style.display = "none";
    }
  } catch (err) {
    if (!silent) {
      loading.style.display = "none";
      showToast("Не удалось загрузить историю файлов", "error");
    }
  }
}

// Sorting & Multi-select State
let currentHistorySort = "date-desc";
let isMultiSelectMode = false;
let selectedHistoryFiles = new Set();

function handleHistorySortChange(val) {
  currentHistorySort = val;
  renderHistoryCards();
}

function toggleMultiSelectMode() {
  isMultiSelectMode = !isMultiSelectMode;
  selectedHistoryFiles.clear();

  const toggleBtn = document.getElementById("btnToggleMultiSelect");
  const toggleText = document.getElementById("multiToggleText");
  const bar = document.getElementById("multiActionBar");

  if (isMultiSelectMode) {
    if (toggleBtn) toggleBtn.classList.add("active");
    if (toggleText) toggleText.innerText = "Отменить выбор";
    if (bar) bar.style.display = "flex";
  } else {
    if (toggleBtn) toggleBtn.classList.remove("active");
    if (toggleText) toggleText.innerText = "Выбрать файлы";
    if (bar) bar.style.display = "none";
  }

  updateMultiSelectCount();
  renderHistoryCards();
}

function updateMultiSelectCount() {
  const countEl = document.getElementById("multiSelectedCount");
  if (countEl) {
    const n = selectedHistoryFiles.size;
    countEl.innerText = `Выбрано: ${n} файл${n === 1 ? '' : (n >= 2 && n <= 4 ? 'а' : 'ов')}`;
  }
}

function selectAllHistoryFiles() {
  allHistoryFiles.forEach(f => selectedHistoryFiles.add(f.filename));
  updateMultiSelectCount();
  renderHistoryCards();
}

function deselectAllHistoryFiles() {
  selectedHistoryFiles.clear();
  updateMultiSelectCount();
  renderHistoryCards();
}

function toggleCardSelection(filename, event) {
  if (event) event.stopPropagation();
  if (selectedHistoryFiles.has(filename)) {
    selectedHistoryFiles.delete(filename);
  } else {
    selectedHistoryFiles.add(filename);
  }
  updateMultiSelectCount();
  renderHistoryCards();
}

// Finder actions
async function openDownloadsFolder() {
  try {
    const res = await fetch("/api/open-folder", { method: "POST" });
    if (res.ok) {
      showToast("Папка загрузок открыта в Finder", "success");
    } else {
      showToast("Не удалось открыть папку", "error");
    }
  } catch (err) {
    showToast("Ошибка: " + err.message, "error");
  }
}

async function revealInFinder(filename) {
  try {
    const res = await fetch(`/api/reveal/${encodeURIComponent(filename)}`, { method: "POST" });
    if (res.ok) {
      showToast("Файл показан в Finder", "success");
    } else {
      showToast("Не удалось показать файл в Finder", "error");
    }
  } catch (err) {
    showToast("Ошибка: " + err.message, "error");
  }
}

// Batch ZIP Download
async function batchDownloadZip() {
  if (selectedHistoryFiles.size === 0) {
    showToast("Выберите хотя бы один файл для архивации", "error");
    return;
  }

  showToast("Создание ZIP архива...", "info");

  try {
    const res = await fetch("/api/batch/zip", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filenames: Array.from(selectedHistoryFiles) })
    });

    if (!res.ok) {
      throw new Error("Не удалось создать ZIP архив");
    }

    const blob = await res.blob();
    const blobUrl = window.URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = blobUrl;
    a.download = `youtube_media_archive_${new Date().toISOString().slice(0, 10)}.zip`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    window.URL.revokeObjectURL(blobUrl);

    showToast("ZIP архив успешно скачан!", "success");
  } catch (err) {
    showToast(err.message, "error");
  }
}

// Batch Delete Selected Files
async function batchDeleteSelected() {
  const count = selectedHistoryFiles.size;
  if (count === 0) {
    showToast("Не выбрано ни одного файла", "error");
    return;
  }

  if (!confirm(`Вы действительно хотите удалить ${count} выбранных файлов?`)) {
    return;
  }

  try {
    const res = await fetch("/api/batch/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filenames: Array.from(selectedHistoryFiles) })
    });

    if (res.ok) {
      const data = await res.json();
      showToast(`Удалено файлов: ${data.deleted_count}`, "success");
      selectedHistoryFiles.clear();
      toggleMultiSelectMode();
      loadHistoryFiles();
    } else {
      throw new Error("Не удалось удалить файлы");
    }
  } catch (err) {
    showToast(err.message, "error");
  }
}

// Helper: Extract original playlist order index from metadata or filename prefix
function getFilePlaylistOrder(f) {
  if (f.playlist_index != null && !isNaN(f.playlist_index)) return Number(f.playlist_index);
  const name = f.basename || f.filename || "";
  const m = name.match(/^(\d+)[\.\s_-]/);
  if (m) return parseInt(m[1], 10);
  return 999999;
}

function getTtlBadgeHtml(item) {
  if (item.is_permanent) {
    return `<span class="card-ttl-badge permanent" title="Вечное хранилище (Без TTL)">♾️ Вечно</span>`;
  }
  const display = item.ttl_display || "⏳ 24ч";
  const badgeClass = display.includes("30") ? "premium" : (display.includes("7") ? "user" : "guest");
  return `<span class="card-ttl-badge ${badgeClass}" title="Срок хранения: ${display}">${display}</span>`;
}

// Render History Cards (YouTube Gallery)
function renderHistoryCards() {
  const empty = document.getElementById("historyEmpty");
  const emptyTitle = document.getElementById("historyEmptyTitle");
  const emptyDesc = document.getElementById("historyEmptyDesc");
  const btnGoTo = document.getElementById("btnGoToDownloader");
  const list = document.getElementById("filesList");
  const subtitle = document.getElementById("historySubtitle");

  // Playlist Detail View Banner
  const playlistBanner = document.getElementById("playlistViewBanner");
  const playlistBannerTitle = document.getElementById("playlistBannerTitle");
  const playlistBannerStats = document.getElementById("playlistBannerStats");

  if (activePlaylistFolder) {
    const pl = allPlaylists.find(p => p.folder_name === activePlaylistFolder);
    if (playlistBanner) playlistBanner.style.display = "flex";
    if (playlistBannerTitle) playlistBannerTitle.innerText = pl ? pl.title : activePlaylistFolder;
    if (playlistBannerStats) {
      const vCount = pl ? pl.video_count : 0;
      const szStr = pl ? pl.total_size_str : "";
      const durStr = pl && pl.total_duration_str ? ` • ${pl.total_duration_str}` : "";
      playlistBannerStats.innerText = `${vCount} файлов • ${szStr}${durStr}`;
    }
    const bannerCoverImg = document.getElementById("playlistBannerCoverImg");
    if (bannerCoverImg) {
      const cUrl = pl ? (pl.thumbnail_url || (pl.first_thumbnail_file ? `/api/thumbnail/${encodeURIComponent(pl.first_thumbnail_file)}` : "")) : "";
      if (cUrl) {
        bannerCoverImg.src = cUrl;
        bannerCoverImg.style.display = "block";
      } else {
        bannerCoverImg.style.display = "none";
      }
    }
  } else {
    if (playlistBanner) playlistBanner.style.display = "none";
  }

  // Handle Playlists Tab (filter === 'playlists' and !activePlaylistFolder)
  if (!activePlaylistFolder && historyFilter === "playlists") {
    let filteredPlaylists = allPlaylists.slice();
    if (historySearchQuery.trim()) {
      const q = historySearchQuery.trim().toLowerCase();
      filteredPlaylists = filteredPlaylists.filter(p => 
        (p.title || "").toLowerCase().includes(q) || 
        (p.folder_name || "").toLowerCase().includes(q)
      );
    }

    if (subtitle) {
      const totalBytes = filteredPlaylists.reduce((acc, p) => acc + (p.total_size_bytes || 0), 0);
      subtitle.innerText = `${filteredPlaylists.length} плейлистов (${formatHumanSize(totalBytes)}) • сохранено локально`;
    }

    if (filteredPlaylists.length === 0) {
      empty.style.display = "block";
      list.innerHTML = "";
      if (allPlaylists.length === 0) {
        if (emptyTitle) emptyTitle.innerText = "Пока нет скачанных плейлистов";
        if (emptyDesc) emptyDesc.innerText = "Вставьте ссылку на плейлист на вкладке «Загрузчик», чтобы скачать его целиком.";
        if (btnGoTo) btnGoTo.style.display = "inline-block";
      } else {
        if (emptyTitle) emptyTitle.innerText = "Ничего не найдено";
        if (emptyDesc) emptyDesc.innerText = "По вашему запросу не нашлось подходящих плейлистов.";
        if (btnGoTo) btnGoTo.style.display = "none";
      }
      return;
    }

    empty.style.display = "none";
    list.innerHTML = "";
    filteredPlaylists.forEach(pl => {
      list.appendChild(createPlaylistCardElement(pl));
    });
    return;
  }

  // File filtering logic
  let filtered = allHistoryFiles.slice();

  if (activePlaylistFolder) {
    filtered = filtered.filter(f => f.playlist_folder === activePlaylistFolder);
  } else if (historyFilter === "video") {
    filtered = filtered.filter(f => f.is_video);
  } else if (historyFilter === "audio") {
    filtered = filtered.filter(f => f.is_audio);
  } else if (historyFilter === "photo") {
    filtered = filtered.filter(f => f.is_photo || f.is_image);
  }

  if (historySearchQuery.trim()) {
    const q = historySearchQuery.trim().toLowerCase();
    filtered = filtered.filter(f => {
      const title = (f.title || "").toLowerCase();
      const fn = (f.filename || "").toLowerCase();
      const quality = (f.quality_tag || "").toLowerCase();
      const ext = (f.ext || "").toLowerCase();
      const plName = (f.playlist_name || "").toLowerCase();
      return title.includes(q) || fn.includes(q) || quality.includes(q) || ext.includes(q) || plName.includes(q);
    });
  }

  // Sort files
  if (activePlaylistFolder && currentHistorySort === "date-desc") {
    // Default inside a playlist: author's intended playlist order (1 -> N)
    filtered.sort((a, b) => {
      const idxA = getFilePlaylistOrder(a);
      const idxB = getFilePlaylistOrder(b);
      if (idxA !== idxB) return idxA - idxB;
      return (a.basename || a.filename || "").localeCompare(b.basename || b.filename || "", undefined, { numeric: true, sensitivity: "base" });
    });
  } else if (currentHistorySort === "date-desc") {
    filtered.sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
  } else if (currentHistorySort === "date-asc") {
    if (activePlaylistFolder) {
      filtered.sort((a, b) => {
        const idxA = getFilePlaylistOrder(a);
        const idxB = getFilePlaylistOrder(b);
        if (idxA !== idxB) return idxA - idxB;
        return (a.created_at || 0) - (b.created_at || 0);
      });
    } else {
      filtered.sort((a, b) => (a.created_at || 0) - (b.created_at || 0));
    }
  } else if (currentHistorySort === "size-desc") {
    filtered.sort((a, b) => (b.size_bytes || 0) - (a.size_bytes || 0));
  } else if (currentHistorySort === "size-asc") {
    filtered.sort((a, b) => (a.size_bytes || 0) - (b.size_bytes || 0));
  } else if (currentHistorySort === "duration-desc") {
    filtered.sort((a, b) => (b.duration_seconds || 0) - (a.duration_seconds || 0));
  } else if (currentHistorySort === "duration-asc") {
    filtered.sort((a, b) => (a.duration_seconds || 0) - (b.duration_seconds || 0));
  } else if (currentHistorySort === "title-asc") {
    filtered.sort((a, b) => (a.title || a.filename || "").localeCompare(b.title || b.filename || "", undefined, { numeric: true, sensitivity: "base" }));
  }

  // Playlists to display alongside files in "all" filter
  let playlistsToDisplay = [];
  if (!activePlaylistFolder && historyFilter === "all") {
    if (historySearchQuery.trim()) {
      const q = historySearchQuery.trim().toLowerCase();
      playlistsToDisplay = allPlaylists.filter(p => 
        (p.title || "").toLowerCase().includes(q) || 
        (p.folder_name || "").toLowerCase().includes(q)
      );
    } else {
      playlistsToDisplay = allPlaylists.slice();
    }
  }

  // In "all" view without search query, hide files that are inside playlists to avoid clutter
  let displayFiles = filtered;
  if (!activePlaylistFolder && historyFilter === "all" && !historySearchQuery.trim() && playlistsToDisplay.length > 0) {
    displayFiles = filtered.filter(f => !f.is_in_playlist);
  }

  // Subtitle summary
  if (subtitle) {
    const totalBytes = allHistoryFiles.reduce((acc, f) => acc + (f.size_bytes || 0), 0);
    const sizeStr = formatHumanSize(totalBytes);
    if (activePlaylistFolder) {
      subtitle.innerText = `${filtered.length} файлов из плейлиста • ${sizeStr} • сохранено локально`;
    } else if (playlistsToDisplay.length > 0 && displayFiles.length > 0) {
      subtitle.innerText = `${playlistsToDisplay.length} плейлистов, ${displayFiles.length} отдельных файлов (${sizeStr}) • сохранено локально`;
    } else {
      subtitle.innerText = `${filtered.length} файлов (${sizeStr}) • сохранено локально`;
    }
  }

  if (playlistsToDisplay.length === 0 && displayFiles.length === 0) {
    empty.style.display = "block";
    list.innerHTML = "";

    if (allHistoryFiles.length === 0 && allPlaylists.length === 0) {
      if (emptyTitle) emptyTitle.innerText = "Пока нет скачанных файлов";
      if (emptyDesc) emptyDesc.innerText = "Вставьте ссылку на медиа или плейлист на вкладке «Загрузчик», чтобы начать скачивание.";
      if (btnGoTo) btnGoTo.style.display = "inline-block";
    } else {
      if (emptyTitle) emptyTitle.innerText = "Ничего не найдено";
      if (emptyDesc) emptyDesc.innerText = "По вашему запросу не нашлось подходящих файлов.";
      if (btnGoTo) btnGoTo.style.display = "none";
    }
    return;
  }

  empty.style.display = "none";
  list.innerHTML = "";

  // 1. Render playlist cards if any
  playlistsToDisplay.forEach(pl => {
    list.appendChild(createPlaylistCardElement(pl));
  });

  // 2. Render file cards
  displayFiles.forEach(f => {
    const card = document.createElement("div");
    const isSelected = selectedHistoryFiles.has(f.filename);
    const isPhoto = Boolean(f.is_photo || f.is_image);
    card.className = `yt-video-card ${isMultiSelectMode ? 'selectable' : ''} ${isSelected ? 'multi-selected' : ''} ${isPhoto ? 'is-photo-card' : ''}`;

    if (isMultiSelectMode) {
      card.onclick = (e) => {
        if (e.target.closest(".yt-card-actions") || e.target.closest("a")) return;
        toggleCardSelection(f.filename, e);
      };
    }

    const dateStr = new Date(f.created_at * 1000).toLocaleString("ru-RU", {
      day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit"
    });

    let qualityClass = "yt-badge-quality";
    const qTag = (f.quality_tag || "").toUpperCase();
    if (isPhoto) {
      qualityClass += " badge-photo";
    } else if (qTag.includes("4K") || qTag.includes("2160")) {
      qualityClass += " badge-4k";
    } else if (qTag.includes("1440") || qTag.includes("2K")) {
      qualityClass += " badge-1440p";
    } else if (qTag.includes("1080")) {
      qualityClass += " badge-1080p";
    } else if (qTag.includes("GIF")) {
      qualityClass += " badge-gif";
    } else if (f.is_audio) {
      qualityClass += " badge-audio";
    }

    const durationBadgeHtml = isPhoto
      ? `<span class="yt-badge-duration">ФОТО</span>`
      : (f.duration_str 
          ? `<span class="yt-badge-duration">${f.duration_str}</span>`
          : `<span class="yt-badge-duration">${f.is_audio ? 'AUDIO' : (qTag.includes('GIF') ? 'GIF' : 'PLAY')}</span>`);

    const cleanTitle = f.title || f.filename;
    const encodedName = encodeURIComponent(f.filename);

    const checkboxHtml = isMultiSelectMode ? `
      <div class="card-checkbox-overlay ${isSelected ? 'checked' : ''}" onclick="toggleCardSelection('${escapeHtml(f.filename)}', event)">
        ${isSelected ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><polyline points="20 6 9 17 4 12"></polyline></svg>' : ''}
      </div>
    ` : "";

    const playlistTagHtml = (f.is_in_playlist && f.playlist_name) ? `
      <span class="yt-card-playlist-tag" onclick="event.stopPropagation(); openPlaylistDetailView('${escapeHtml(f.playlist_folder)}')" title="Открыть плейлист «${escapeHtml(f.playlist_name)}»">
        <svg viewBox="0 0 24 24" width="12" height="12" fill="currentColor"><path d="M4 6H2v14c0 1.1.9 2 2 2h14v-2H4V6zm16-4H8c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm0 14H8V4h12v12zm-8-2 6-4-6-4v8z"/></svg>
        <span>${f.playlist_index ? '#' + f.playlist_index + ' ' : ''}${escapeHtml(f.playlist_name)}</span>
      </span>
    ` : "";

    const playActionBtn = isPhoto ? `
      <button type="button" class="btn-yt-play" onclick="openLightboxModal('${encodedName}')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line><line x1="11" y1="8" x2="11" y2="14"></line><line x1="8" y1="11" x2="14" y2="11"></line></svg>
        <span>Просмотр</span>
      </button>
    ` : `
      <button type="button" class="btn-yt-play" onclick="openPlayerModal('${encodedName}')">
        <svg viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>
        <span>Смотреть</span>
      </button>
    `;

    const thumbClickAction = isPhoto ? `openLightboxModal('${encodedName}')` : `handleThumbClick(this, '${encodedName}')`;
    const titleClickAction = isPhoto ? `openLightboxModal('${encodedName}')` : `openPlayerModal('${encodedName}')`;
    const thumbOverlayIcon = isPhoto ? `
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="28" height="28"><circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line><line x1="11" y1="8" x2="11" y2="14"></line><line x1="8" y1="11" x2="14" y2="11"></line></svg>
    ` : `
      <svg viewBox="0 0 24 24" fill="currentColor"><polygon points="6 4 20 12 6 20 6 4"/></svg>
    `;

    card.innerHTML = `
      <div class="yt-thumb-wrapper" onclick="${thumbClickAction}" title="${isPhoto ? 'Нажмите для просмотра фото' : 'Нажмите для воспроизведения'}">
        ${checkboxHtml}
        <img class="yt-thumb-img" src="/api/thumbnail/${encodedName}" alt="${escapeHtml(cleanTitle)}" loading="lazy" onerror="this.src='/api/thumbnail/${encodedName}'">
        <div class="yt-thumb-gradient"></div>
        <span class="${qualityClass}">${escapeHtml(f.quality_tag || f.ext)}</span>
        ${durationBadgeHtml}
        <div class="yt-thumb-overlay">
          <div class="yt-play-icon">
            ${thumbOverlayIcon}
          </div>
        </div>
      </div>

      <div class="yt-card-content">
        ${playlistTagHtml}
        <h4 class="yt-card-title" onclick="${titleClickAction}" title="${escapeHtml(cleanTitle)}">${escapeHtml(cleanTitle)}</h4>
        <div class="yt-card-meta-row">
          <span class="yt-meta-format ${f.is_audio ? 'audio' : (isPhoto ? 'photo' : '')}">${f.ext}</span>
          <span class="yt-meta-dot">&bull;</span>
          <span>${f.size_str}</span>
          <span class="yt-meta-dot">&bull;</span>
          <span>${dateStr}</span>
          <span class="yt-meta-dot">&bull;</span>
          ${getTtlBadgeHtml(f)}
        </div>

        <div class="yt-card-actions">
          ${playActionBtn}
          <button type="button" class="btn-card-reveal" title="Показать в Finder на Mac" onclick="revealInFinder('${escapeHtml(f.filename)}')">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg>
            <span>Finder</span>
          </button>
          <a href="/api/files/${encodedName}" download="${f.filename}" class="btn-yt-download" title="Сохранить файл на диск">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>
          </a>
          <button type="button" class="btn-yt-delete" title="Удалить файл" onclick="deleteHistoryFile('${encodedName}')">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
          </button>
        </div>
      </div>
    `;

    list.appendChild(card);
    if (!isMultiSelectMode && !isPhoto && !f.is_audio) {
      attachHoverPreview(card, f);
    }
  });
}

// Playlist Card Element Creator
function createPlaylistCardElement(pl) {
  const card = document.createElement("div");
  card.className = "yt-video-card yt-playlist-card";
  card.onclick = () => openPlaylistDetailView(pl.folder_name);

  const cleanTitle = pl.title || pl.folder_name;
  const thumbTarget = pl.first_thumbnail_file || pl.first_video_file || "";
  const thumbUrl = pl.thumbnail_url || (thumbTarget ? `/api/thumbnail/${encodeURIComponent(thumbTarget)}` : "");
  const itemCount = pl.video_count !== undefined ? pl.video_count : (pl.item_count !== undefined ? pl.item_count : (pl.files ? pl.files.length : 0));

  card.innerHTML = `
    <div class="yt-thumb-wrapper yt-playlist-thumb" title="Открыть плейлист «${escapeHtml(cleanTitle)}»">
      <img class="yt-thumb-img" src="${thumbUrl}" alt="${escapeHtml(cleanTitle)}" loading="lazy" onerror="this.style.opacity='0.4'">
      <div class="yt-playlist-overlay-side">
        <svg viewBox="0 0 24 24" fill="currentColor"><path d="M4 6H2v14c0 1.1.9 2 2 2h14v-2H4V6zm16-4H8c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm0 14H8V4h12v12zm-8-2 6-4-6-4v8z"/></svg>
        <span class="pl-overlay-count">${itemCount}</span>
        <span class="pl-overlay-text">ФАЙЛОВ</span>
      </div>
      <span class="playlist-card-pill">📁 ПЛЕЙЛИСТ</span>
    </div>

    <div class="yt-card-content">
      <h4 class="yt-card-title" title="${escapeHtml(cleanTitle)}">${escapeHtml(cleanTitle)}</h4>
      <div class="yt-card-meta-row">
        <span class="yt-meta-format">${itemCount} файлов</span>
        <span class="yt-meta-dot">&bull;</span>
        <span>${pl.total_size_str}</span>
        ${pl.total_duration_str ? `<span class="yt-meta-dot">&bull;</span><span>${pl.total_duration_str}</span>` : ''}
        <span class="yt-meta-dot">&bull;</span>
        ${getTtlBadgeHtml(pl)}
      </div>

      <div class="yt-card-actions">
        <button type="button" class="btn-yt-play" title="Открыть плейлист" onclick="event.stopPropagation(); openPlaylistDetailView('${escapeHtml(pl.folder_name)}')">
          <svg viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>
          <span>Открыть</span>
        </button>
        <button type="button" class="btn-card-reveal" title="Показать папку в Finder на Mac" onclick="event.stopPropagation(); openPlaylistInFinder('${escapeHtml(pl.folder_name)}')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg>
          <span>Finder</span>
        </button>
        <button type="button" class="btn-yt-download" title="Скачать ZIP архивом" onclick="event.stopPropagation(); downloadPlaylistZipDirect('${escapeHtml(pl.folder_name)}')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>
        </button>
        <button type="button" class="btn-yt-delete" title="Удалить плейлист" onclick="event.stopPropagation(); deletePlaylistFolder('${escapeHtml(pl.folder_name)}')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
        </button>
      </div>
    </div>
  `;
  return card;
}

// Playlist Navigation & Actions
function openPlaylistDetailView(folderName) {
  activePlaylistFolder = folderName;
  historyFilter = "all";
  document.querySelectorAll(".history-filter-pill").forEach(p => p.classList.remove("active"));
  const pill = document.getElementById("filterPillAll");
  if (pill) pill.classList.add("active");
  renderHistoryCards();
  const banner = document.getElementById("playlistViewBanner");
  if (banner) banner.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function exitPlaylistView(shouldRerender = true) {
  activePlaylistFolder = null;
  const banner = document.getElementById("playlistViewBanner");
  if (banner) banner.style.display = "none";
  if (shouldRerender) {
    renderHistoryCards();
  }
}

async function openPlaylistInFinder(folderName) {
  try {
    const res = await fetch("/api/open-folder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ subfolder: folderName })
    });
    if (res.ok) {
      showToast("Папка плейлиста открыта в Finder", "success");
    } else {
      showToast("Не удалось открыть папку плейлиста", "error");
    }
  } catch (err) {
    showToast("Ошибка: " + err.message, "error");
  }
}

function downloadPlaylistZipDirect(folderName) {
  showToast("Подготовка ZIP архива плейлиста...", "info");
  const a = document.createElement("a");
  a.href = `/api/playlist/zip/${encodeURIComponent(folderName)}`;
  a.download = `${folderName}.zip`;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

async function deletePlaylistFolder(folderName) {
  const pl = allPlaylists.find(p => p.folder_name === folderName);
  const title = pl ? pl.title : folderName;
  const count = pl ? pl.video_count : "";
  if (!confirm(`Вы действительно хотите удалить плейлист:\n«${title}» (${count} файлов)?\n\nВсе файлы и папка будут удалены.`)) {
    return;
  }

  try {
    const res = await fetch(`/api/playlist/${encodeURIComponent(folderName)}`, {
      method: "DELETE"
    });
    if (res.ok) {
      showToast(`Плейлист «${title}» удалён`, "info");
      if (activePlaylistFolder === folderName) {
        exitPlaylistView(false);
      }
      loadHistoryFiles();
    } else {
      throw new Error();
    }
  } catch (err) {
    showToast("Не удалось удалить плейлист", "error");
  }
}

async function openCurrentPlaylistFolder() {
  if (!activePlaylistFolder) return;
  openPlaylistInFinder(activePlaylistFolder);
}

async function downloadCurrentPlaylistZip() {
  if (!activePlaylistFolder) return;
  downloadPlaylistZipDirect(activePlaylistFolder);
}

async function deleteCurrentPlaylist() {
  if (!activePlaylistFolder) return;
  deletePlaylistFolder(activePlaylistFolder);
}

// Playlist Sequential Playback in Player Modal
function playAllCurrentPlaylist() {
  if (!activePlaylistFolder) return;
  const plFiles = allHistoryFiles.filter(f => f.playlist_folder === activePlaylistFolder);
  if (plFiles.length === 0) {
    showToast("В плейлисте нет файлов для воспроизведения", "warning");
    return;
  }
  currentPlaylistQueue = plFiles.slice();
  currentPlaylistQueue.sort((a, b) => {
    const idxA = getFilePlaylistOrder(a);
    const idxB = getFilePlaylistOrder(b);
    if (idxA !== idxB) return idxA - idxB;
    return (a.basename || a.filename || "").localeCompare(b.basename || b.filename || "", undefined, { numeric: true, sensitivity: "base" });
  });
  openPlayerModalWithQueue(0);
}

function openPlayerModalWithQueue(index) {
  if (index < 0 || index >= currentPlaylistQueue.length) return;
  currentPlaylistQueueIndex = index;
  const file = currentPlaylistQueue[index];
  openPlayerModal(encodeURIComponent(file.filename));
  updatePlayerModalQueueUI();
}

function playPrevInPlaylist() {
  if (currentPlaylistQueueIndex > 0) {
    openPlayerModalWithQueue(currentPlaylistQueueIndex - 1);
  }
}

function playNextInPlaylist() {
  if (currentPlaylistQueueIndex >= 0 && currentPlaylistQueueIndex < currentPlaylistQueue.length - 1) {
    openPlayerModalWithQueue(currentPlaylistQueueIndex + 1);
  }
}

function updatePlayerModalQueueUI() {
  const nav = document.getElementById("modalPlaylistNav");
  const counter = document.getElementById("plNavCounter");
  const prevBtn = document.getElementById("btnPlayerPrev");
  const nextBtn = document.getElementById("btnPlayerNext");
  const badge = document.getElementById("modalPlaylistBadge");

  if (currentPlaylistQueue.length > 0 && currentPlaylistQueueIndex >= 0) {
    const curFile = currentPlaylistQueue[currentPlaylistQueueIndex];
    const isPl = Boolean(curFile.playlist_folder || activePlaylistFolder);
    const plName = curFile.playlist_name || activePlaylistFolder || (historyFilter === "video" ? "Все видео" : (historyFilter === "audio" ? "Вся музыка" : "Медиатека"));
    if (nav) nav.style.display = currentPlaylistQueue.length > 1 ? "flex" : "none";
    if (counter) counter.innerText = `${currentPlaylistQueueIndex + 1} / ${currentPlaylistQueue.length}`;
    if (prevBtn) {
      prevBtn.disabled = currentPlaylistQueueIndex === 0;
      prevBtn.style.opacity = currentPlaylistQueueIndex === 0 ? "0.35" : "1";
    }
    if (nextBtn) {
      nextBtn.disabled = currentPlaylistQueueIndex === currentPlaylistQueue.length - 1;
      nextBtn.style.opacity = (currentPlaylistQueueIndex === currentPlaylistQueue.length - 1) ? "0.35" : "1";
    }
    if (badge) {
      const icon = isPl ? '<path d="M4 6H2v14c0 1.1.9 2 2 2h14v-2H4V6zm16-4H8c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm0 14H8V4h12v12zm-8-2 6-4-6-4v8z"/>' : '<polygon points="5 3 19 12 5 21 5 3"/>';
      badge.innerHTML = `<svg viewBox="0 0 24 24" width="13" height="13" fill="currentColor">${icon}</svg> <span>${escapeHtml(plName)}</span>`;
      badge.style.display = "inline-flex";
    }
  } else {
    if (nav) nav.style.display = "none";
    if (badge) badge.style.display = "none";
  }
}

// YouTube Live Video Hover Preview System
function attachHoverPreview(card, file) {
  if (!file.is_video) return;

  const wrapper = card.querySelector(".yt-thumb-wrapper");
  if (!wrapper) return;

  let hoverTimer = null;

  card.addEventListener("mouseenter", () => {
    // 250ms debounce to prevent flicker on rapid mouse movement
    hoverTimer = setTimeout(() => {
      startHoverPreview(wrapper, file);
    }, 250);
  });

  card.addEventListener("mouseleave", () => {
    if (hoverTimer) {
      clearTimeout(hoverTimer);
      hoverTimer = null;
    }
    stopHoverPreview(wrapper);
  });
}

function startHoverPreview(wrapper, file) {
  // If already playing or loading, do not recreate
  if (wrapper.querySelector(".yt-preview-video")) return;

  const video = document.createElement("video");
  video.className = "yt-preview-video";
  video.muted = true;
  video.playsInline = true;
  video.autoplay = true;
  video.loop = true;
  video.preload = "auto";

  const streamUrl = `/api/stream/${encodeURIComponent(file.filename)}`;
  video.src = streamUrl;

  // Jump past title/black intro: 5-8% into the video (max 15s) or 0.5s for short clips
  const dur = file.duration_seconds || 0;
  const startSec = dur > 8 ? Math.min(Math.max(1.5, dur * 0.08), 15) : 0.2;
  video.currentTime = startSec;

  // Red progress scrubber
  const progressBar = document.createElement("div");
  progressBar.className = "yt-preview-progress-bar";
  const progressFill = document.createElement("div");
  progressFill.className = "yt-preview-progress-fill";
  progressBar.appendChild(progressFill);

  // Mute badge
  const muteBadge = document.createElement("div");
  muteBadge.className = "yt-preview-mute-badge";
  muteBadge.title = "Предпросмотр без звука";
  muteBadge.innerHTML = `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"></polygon><line x1="23" y1="9" x2="17" y2="15"></line><line x1="17" y1="9" x2="23" y2="15"></line></svg>`;

  video.addEventListener("timeupdate", () => {
    if (video.duration) {
      const pct = (video.currentTime / video.duration) * 100;
      progressFill.style.width = `${pct}%`;
    }
  });

  video.addEventListener("playing", () => {
    video.classList.add("playing");
    wrapper.classList.add("preview-active");
  });

  video.addEventListener("error", () => {
    stopHoverPreview(wrapper);
  });

  wrapper.appendChild(video);
  wrapper.appendChild(progressBar);
  wrapper.appendChild(muteBadge);

  const playPromise = video.play();
  if (playPromise !== undefined) {
    playPromise.catch(() => {});
  }
}

function stopHoverPreview(wrapper) {
  wrapper.classList.remove("preview-active");

  const video = wrapper.querySelector(".yt-preview-video");
  const progressBar = wrapper.querySelector(".yt-preview-progress-bar");
  const muteBadge = wrapper.querySelector(".yt-preview-mute-badge");

  if (video) {
    video.pause();
    video.removeAttribute("src");
    video.load();
    video.remove();
  }
  if (progressBar) progressBar.remove();
  if (muteBadge) muteBadge.remove();
}

function handleThumbClick(wrapper, encodedFilename) {
  if (isMultiSelectMode) return;
  const video = wrapper.querySelector(".yt-preview-video");
  const curTime = (video && video.currentTime > 0) ? video.currentTime : null;
  stopHoverPreview(wrapper);
  openPlayerModal(encodedFilename, curTime);
}

function formatHumanSize(bytes) {
  if (!bytes || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let val = bytes;
  while (val >= 1024 && i < units.length - 1) {
    val /= 1024;
    i++;
  }
  return `${val.toFixed(1)} ${units[i]}`;
}

// Filter & Search Functions
function setHistoryFilter(filter) {
  if (activePlaylistFolder && filter !== "playlists") {
    exitPlaylistView(false);
  }
  historyFilter = filter;
  document.querySelectorAll(".history-filter-pill").forEach(p => p.classList.remove("active"));
  let pillId = "filterPillAll";
  if (filter === "playlists") pillId = "filterPillPlaylists";
  else if (filter === "video") pillId = "filterPillVideo";
  else if (filter === "audio") pillId = "filterPillAudio";
  else if (filter === "photo") pillId = "filterPillPhotos";

  const activePill = document.getElementById(pillId);
  if (activePill) activePill.classList.add("active");
  renderHistoryCards();
}

function handleHistorySearch(val) {
  historySearchQuery = val;
  const btnClear = document.getElementById("btnClearSearch");
  if (btnClear) {
    btnClear.style.display = val ? "flex" : "none";
  }
  renderHistoryCards();
}

function clearHistorySearch() {
  const input = document.getElementById("historySearchInput");
  if (input) input.value = "";
  handleHistorySearch("");
}

function setHistoryViewMode(mode) {
  historyViewMode = mode;
  localStorage.setItem("yt_history_view", mode);
  const gridBtn = document.getElementById("btnViewGrid");
  const listBtn = document.getElementById("btnViewList");
  const container = document.getElementById("filesList");

  if (mode === "list") {
    if (gridBtn) gridBtn.classList.remove("active");
    if (listBtn) listBtn.classList.add("active");
    if (container) container.classList.add("list-view");
  } else {
    if (gridBtn) gridBtn.classList.add("active");
    if (listBtn) listBtn.classList.remove("active");
    if (container) container.classList.remove("list-view");
  }
}

// Delete History File
async function deleteHistoryFile(encodedFilename) {
  const filename = decodeURIComponent(encodedFilename);
  if (!confirm(`Вы действительно хотите удалить файл:\n"${filename}"?`)) {
    return;
  }

  try {
    let res = await fetch(`/api/history/${encodeURIComponent(filename)}`, {
      method: "DELETE"
    });

    // Fallback to POST /api/batch/delete if path routing had issues with encoded characters
    if (!res.ok) {
      res = await fetch("/api/batch/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filenames: [filename] })
      });
    }

    if (res.ok) {
      showToast("Файл удалён с диска", "info");
      loadHistoryFiles();
    } else {
      throw new Error();
    }
  } catch (err) {
    showToast("Не удалось удалить файл с диска", "error");
  }
}

// Media Player Modal with Subtitles & Playlist Queue Support
async function openPlayerModal(encodedFilename, startTime = null) {
  const filename = decodeURIComponent(encodedFilename);
  const modal = document.getElementById("playerModal");
  const modalTitle = document.getElementById("modalPlayerTitle");
  const videoPlayer = document.getElementById("modalVideoPlayer");
  const audioPlayer = document.getElementById("modalAudioPlayer");
  const subsBadge = document.getElementById("modalSubsBadge");

  const foundFile = allHistoryFiles.find(f => f.filename === filename);
  modalTitle.innerText = (foundFile && foundFile.title) ? foundFile.title : filename;
  const streamUrl = `/api/stream/${encodeURIComponent(filename)}`;

  // Manage playlist/library queue in modal
  if (foundFile && foundFile.playlist_folder) {
    if (!currentPlaylistQueue || currentPlaylistQueue.length === 0 || currentPlaylistQueue[0].playlist_folder !== foundFile.playlist_folder) {
      currentPlaylistQueue = allHistoryFiles.filter(f => f.playlist_folder === foundFile.playlist_folder);
      currentPlaylistQueue.sort((a, b) => {
        const idxA = getFilePlaylistOrder(a);
        const idxB = getFilePlaylistOrder(b);
        if (idxA !== idxB) return idxA - idxB;
        return (a.basename || a.filename || "").localeCompare(b.basename || b.filename || "", undefined, { numeric: true, sensitivity: "base" });
      });
    }
    currentPlaylistQueueIndex = currentPlaylistQueue.findIndex(f => f.filename === filename);
  } else {
    let playable = allHistoryFiles.filter(f => f.is_video || f.is_audio);
    if (activePlaylistFolder) {
      playable = playable.filter(f => f.playlist_folder === activePlaylistFolder);
      playable.sort((a, b) => {
        const idxA = getFilePlaylistOrder(a);
        const idxB = getFilePlaylistOrder(b);
        if (idxA !== idxB) return idxA - idxB;
        return (a.basename || a.filename || "").localeCompare(b.basename || b.filename || "", undefined, { numeric: true, sensitivity: "base" });
      });
    } else if (historyFilter === "video") {
      playable = playable.filter(f => f.is_video);
    } else if (historyFilter === "audio") {
      playable = playable.filter(f => f.is_audio);
    }
    currentPlaylistQueue = playable;
    currentPlaylistQueueIndex = currentPlaylistQueue.findIndex(f => f.filename === filename);
  }
  updatePlayerModalQueueUI();

  const ext = filename.split('.').pop().toLowerCase();
  const isAudio = ["mp3", "m4a", "wav", "flac", "opus", "aac", "ogg"].includes(ext);

  if (isAudio) {
    videoPlayer.style.display = "none";
    videoPlayer.pause();
    videoPlayer.src = "";
    videoPlayer.onended = null;
    if (subsBadge) subsBadge.style.display = "none";

    audioPlayer.src = streamUrl;
    audioPlayer.style.display = "block";
    audioPlayer.onended = () => {
      if (currentPlaylistQueue.length > 0 && currentPlaylistQueueIndex >= 0 && currentPlaylistQueueIndex < currentPlaylistQueue.length - 1) {
        playNextInPlaylist();
      }
    };
    audioPlayer.play();
  } else {
    audioPlayer.style.display = "none";
    audioPlayer.pause();
    audioPlayer.src = "";
    audioPlayer.onended = null;

    // Clear previous subtitle tracks
    while (videoPlayer.firstChild) {
      videoPlayer.removeChild(videoPlayer.firstChild);
    }

    videoPlayer.src = streamUrl;
    videoPlayer.style.display = "block";
    videoPlayer.onended = () => {
      if (currentPlaylistQueue.length > 0 && currentPlaylistQueueIndex >= 0 && currentPlaylistQueueIndex < currentPlaylistQueue.length - 1) {
        playNextInPlaylist();
      }
    };

    // Probe subtitles asynchronously
    if (subsBadge) subsBadge.style.display = "none";
    try {
      const resSubs = await fetch(`/api/subtitles/${encodeURIComponent(filename)}`);
      if (resSubs.ok) {
        const dataSubs = await resSubs.json();
        const subs = dataSubs.subtitles || [];
        if (subs.length > 0) {
          if (subsBadge) {
            subsBadge.innerText = `CC Субтитры (${subs.length})`;
            subsBadge.style.display = "inline-flex";
          }
          subs.forEach((sub, sIdx) => {
            const track = document.createElement("track");
            track.kind = "subtitles";
            track.label = sub.label || sub.name || `Субтитры ${sIdx + 1}`;
            track.srclang = sub.lang || "ru";
            track.src = sub.url || `/api/subtitles-track?filename=${encodeURIComponent(filename)}&track_id=${encodeURIComponent(sub.id)}`;
            if (sIdx === 0 || sub.lang === "ru" || sub.lang === "rus") {
              track.default = true;
            }
            videoPlayer.appendChild(track);
          });
        }
      }
    } catch (e) {
      console.warn("Subtitles probe error", e);
    }

    if (startTime && startTime > 0) {
      videoPlayer.currentTime = startTime;
    }
    videoPlayer.play();
  }

  modal.style.display = "flex";
}

function closePlayerModal(event) {
  if (event && event.target && event.target.id !== "playerModal" && !event.target.closest(".btn-modal-close")) {
    return;
  }
  const modal = document.getElementById("playerModal");
  const videoPlayer = document.getElementById("modalVideoPlayer");
  const audioPlayer = document.getElementById("modalAudioPlayer");

  videoPlayer.pause();
  videoPlayer.src = "";
  videoPlayer.onended = null;
  while (videoPlayer.firstChild) {
    videoPlayer.removeChild(videoPlayer.firstChild);
  }
  audioPlayer.pause();
  audioPlayer.src = "";
  audioPlayer.onended = null;

  modal.style.display = "none";
}

// Full-resolution Lightbox Photo Viewer with Prev/Next Navigation
let currentLightboxFilename = null;
let currentPhotoQueue = [];
let currentPhotoQueueIndex = -1;

function openLightboxModal(encodedFilename) {
  const filename = decodeURIComponent(encodedFilename);
  currentLightboxFilename = filename;

  // Build photo queue from currently filtered/visible photos
  let photoFiles = allHistoryFiles.filter(f => f.is_photo || f.is_image);
  if (activePlaylistFolder) {
    photoFiles = photoFiles.filter(f => f.playlist_folder === activePlaylistFolder);
  }
  currentPhotoQueue = photoFiles;
  currentPhotoQueueIndex = currentPhotoQueue.findIndex(f => f.filename === filename);
  if (currentPhotoQueueIndex === -1 && photoFiles.length > 0) {
    currentPhotoQueueIndex = 0;
  }

  updateLightboxUI();
}

function updateLightboxUI() {
  const modal = document.getElementById("lightboxModal");
  const img = document.getElementById("lightboxImg");
  const title = document.getElementById("lightboxTitle");
  const counter = document.getElementById("lightboxCounter");
  const metaRes = document.getElementById("lightboxMetaRes");
  const metaSize = document.getElementById("lightboxMetaSize");
  const downloadBtn = document.getElementById("lightboxDownloadBtn");
  const prevBtn = document.getElementById("btnLightboxPrev");
  const nextBtn = document.getElementById("btnLightboxNext");

  if (!modal || !img) return;

  if (currentPhotoQueue.length > 0 && currentPhotoQueueIndex >= 0) {
    const foundFile = currentPhotoQueue[currentPhotoQueueIndex];
    currentLightboxFilename = foundFile.filename;

    if (title) title.innerText = foundFile.title || foundFile.filename;
    if (counter) {
      counter.innerText = `${currentPhotoQueueIndex + 1} / ${currentPhotoQueue.length}`;
      counter.style.display = currentPhotoQueue.length > 1 ? "inline-block" : "none";
    }
    if (metaRes) metaRes.innerText = foundFile.quality_tag || "HQ";
    if (metaSize) metaSize.innerText = foundFile.size_str || "";
    if (downloadBtn) {
      downloadBtn.href = `/api/files/${encodeURIComponent(foundFile.filename)}`;
      downloadBtn.download = foundFile.filename;
    }

    img.src = `/api/files/${encodeURIComponent(foundFile.filename)}`;

    if (prevBtn) {
      prevBtn.style.display = currentPhotoQueue.length > 1 ? "flex" : "none";
      prevBtn.disabled = currentPhotoQueueIndex === 0;
    }
    if (nextBtn) {
      nextBtn.style.display = currentPhotoQueue.length > 1 ? "flex" : "none";
      nextBtn.disabled = currentPhotoQueueIndex === currentPhotoQueue.length - 1;
    }
  }

  modal.style.display = "flex";
}

function showPrevPhoto() {
  if (currentPhotoQueueIndex > 0) {
    currentPhotoQueueIndex--;
    updateLightboxUI();
  }
}

function showNextPhoto() {
  if (currentPhotoQueueIndex >= 0 && currentPhotoQueueIndex < currentPhotoQueue.length - 1) {
    currentPhotoQueueIndex++;
    updateLightboxUI();
  }
}

function closeLightboxModal(event) {
  if (event && event.target && event.target.id !== "lightboxModal" && !event.target.closest(".btn-modal-close")) {
    return;
  }
  const modal = document.getElementById("lightboxModal");
  const img = document.getElementById("lightboxImg");
  if (img) img.src = "";
  if (modal) modal.style.display = "none";
  currentLightboxFilename = null;
  currentPhotoQueue = [];
  currentPhotoQueueIndex = -1;
}

function downloadCurrentLightboxPhoto() {
  if (!currentLightboxFilename) return;
  const a = document.createElement("a");
  a.href = `/api/files/${encodeURIComponent(currentLightboxFilename)}`;
  a.download = currentLightboxFilename;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function revealCurrentLightboxInFinder() {
  if (!currentLightboxFilename) return;
  revealInFinder(currentLightboxFilename);
}

// Global Keyboard Navigation (Arrow keys and Escape for Modals, Player, and Lightbox)
document.addEventListener("keydown", (e) => {
  const authModal = document.getElementById("authModal");
  const cabinetModal = document.getElementById("cabinetModal");
  if (authModal && authModal.style.display === "flex" && e.key === "Escape") {
    closeAuthModal();
    return;
  }
  if (cabinetModal && cabinetModal.style.display === "flex" && e.key === "Escape") {
    closeCabinetModal();
    return;
  }

  const playerModal = document.getElementById("playerModal");
  const lightboxModal = document.getElementById("lightboxModal");

  // Handle Photo Lightbox Modal
  if (lightboxModal && lightboxModal.style.display === "flex") {
    if (e.key === "Escape") {
      closeLightboxModal();
    } else if (e.key === "ArrowLeft") {
      e.preventDefault();
      showPrevPhoto();
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      showNextPhoto();
    }
    return;
  }

  // Handle Video/Audio Player Modal
  if (playerModal && playerModal.style.display === "flex") {
    if (e.key === "Escape") {
      closePlayerModal();
    } else if (e.key === "ArrowLeft" || e.key === "[") {
      if (!e.altKey && !e.metaKey) {
        e.preventDefault();
        playPrevInPlaylist();
      }
    } else if (e.key === "ArrowRight" || e.key === "]") {
      if (!e.altKey && !e.metaKey) {
        e.preventDefault();
        playNextInPlaylist();
      }
    }
    return;
  }

  // Handle Theme dropdown close on Escape
  if (e.key === "Escape") {
    closeThemeDropdown();
  }
});

// ==========================================================================
// Multi-Theme & Dark/Light Mode Management System
// ==========================================================================
const THEME_NAMES = {
  cyan: "Electric Cyan",
  monochrome: "Monochrome",
  indigo: "Nordic Indigo",
  emerald: "Cyber Emerald",
  crimson: "Crimson Classic"
};

function getAppTheme() {
  try {
    return localStorage.getItem("mediapull_theme") || "cyan";
  } catch (e) {
    return "cyan";
  }
}

function getAppMode() {
  try {
    return localStorage.getItem("mediapull_mode") || "dark";
  } catch (e) {
    return "dark";
  }
}

function initAppTheme() {
  const theme = getAppTheme();
  const mode = getAppMode();

  applyThemeDOM(theme, false);
  applyModeDOM(mode, false);

  // Close dropdown on click outside
  document.addEventListener("click", (e) => {
    const container = document.getElementById("themeMenuContainer");
    if (container && !container.contains(e.target)) {
      closeThemeDropdown();
    }
  });
}

function applyThemeDOM(theme, notify = false) {
  if (!THEME_NAMES[theme]) theme = "cyan";
  document.documentElement.setAttribute("data-theme", theme);
  try {
    localStorage.setItem("mediapull_theme", theme);
  } catch (e) {}

  // Update theme option items in navbar dropdown
  const themeItems = document.querySelectorAll(".theme-option-item");
  themeItems.forEach((btn) => {
    const val = btn.getAttribute("data-theme-val");
    if (val === theme) {
      btn.classList.add("active");
    } else {
      btn.classList.remove("active");
    }
  });

  // Update cabinet pills
  const cabPills = document.querySelectorAll(".cabinet-theme-pill");
  cabPills.forEach((pill) => {
    const val = pill.getAttribute("data-cab-theme");
    if (val === theme) {
      pill.classList.add("active");
    } else {
      pill.classList.remove("active");
    }
  });

  // Update navbar theme dot indicator color
  const dot = document.getElementById("navbarThemeDot");
  if (dot) {
    const isLight = getAppMode() === "light";
    const dotColors = {
      cyan: isLight ? "#0284c7" : "#00d2ff",
      monochrome: isLight ? "#0f172a" : "#ffffff",
      indigo: isLight ? "#4f46e5" : "#6366f1",
      emerald: isLight ? "#059669" : "#10b981",
      crimson: isLight ? "#e11d48" : "#ff0033"
    };
    dot.style.background = dotColors[theme] || "#00d2ff";
  }

  if (notify && typeof showToast === "function") {
    showToast(`Палитра: ${THEME_NAMES[theme]}`, "info");
  }
}

function applyModeDOM(mode, notify = false) {
  if (mode !== "light" && mode !== "dark") mode = "dark";
  document.documentElement.setAttribute("data-mode", mode);
  try {
    localStorage.setItem("mediapull_mode", mode);
  } catch (e) {}

  // Update navbar mode icon
  const iconMoon = document.getElementById("iconMoonSvg");
  const iconSun = document.getElementById("iconSunSvg");
  if (iconMoon && iconSun) {
    if (mode === "light") {
      iconMoon.style.display = "none";
      iconSun.style.display = "inline-block";
    } else {
      iconMoon.style.display = "inline-block";
      iconSun.style.display = "none";
    }
  }

  // Update dropdown mode buttons
  const btnDark = document.getElementById("modeBtnDark");
  const btnLight = document.getElementById("modeBtnLight");
  if (btnDark && btnLight) {
    if (mode === "dark") {
      btnDark.classList.add("active");
      btnLight.classList.remove("active");
    } else {
      btnDark.classList.remove("active");
      btnLight.classList.add("active");
    }
  }

  // Update cabinet mode buttons
  const cabDark = document.getElementById("cabModeBtnDark");
  const cabLight = document.getElementById("cabModeBtnLight");
  if (cabDark && cabLight) {
    if (mode === "dark") {
      cabDark.classList.add("active");
      cabLight.classList.remove("active");
    } else {
      cabDark.classList.remove("active");
      cabLight.classList.add("active");
    }
  }

  // Update dropdown header mode badge
  const modeBadge = document.getElementById("themeHeaderModeBadge");
  if (modeBadge) {
    modeBadge.textContent = mode === "light" ? "Светлая тема" : "Тёмная тема";
  }

  // Update dot in case theme requires mode-specific dot
  const theme = getAppTheme();
  const dot = document.getElementById("navbarThemeDot");
  if (dot) {
    const isLight = mode === "light";
    const dotColors = {
      cyan: isLight ? "#0284c7" : "#00d2ff",
      monochrome: isLight ? "#0f172a" : "#ffffff",
      indigo: isLight ? "#4f46e5" : "#6366f1",
      emerald: isLight ? "#059669" : "#10b981",
      crimson: isLight ? "#e11d48" : "#ff0033"
    };
    dot.style.background = dotColors[theme] || "#00d2ff";
  }

  if (notify && typeof showToast === "function") {
    showToast(mode === "light" ? "Светлая тема включена" : "Тёмная тема включена", "info");
  }
}

function setAppTheme(themeName) {
  applyThemeDOM(themeName, true);
}

function setAppMode(modeName) {
  applyModeDOM(modeName, true);
}

function toggleThemeDropdown(e) {
  if (e) {
    e.stopPropagation();
    e.preventDefault();
  }
  const dropdown = document.getElementById("themeDropdownMenu");
  const btn = document.getElementById("btnThemeMenu");
  if (!dropdown || !btn) return;

  const isOpen = dropdown.classList.contains("show");
  if (isOpen) {
    closeThemeDropdown();
  } else {
    dropdown.classList.add("show");
    btn.setAttribute("aria-expanded", "true");
    btn.classList.add("active");
  }
}

function closeThemeDropdown() {
  const dropdown = document.getElementById("themeDropdownMenu");
  const btn = document.getElementById("btnThemeMenu");
  if (dropdown) dropdown.classList.remove("show");
  if (btn) {
    btn.setAttribute("aria-expanded", "false");
    btn.classList.remove("active");
  }
}
