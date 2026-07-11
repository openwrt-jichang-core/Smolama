const loginForm = document.getElementById('loginForm');
const loginPassword = document.getElementById('loginPassword');
const loginSubmit = document.getElementById('loginSubmit');
const loginError = document.getElementById('loginError');

let lockdownTimer = null;

function setError(msg) {
  loginError.textContent = msg || '';
}

function startLockCountdown(seconds) {
  clearInterval(lockdownTimer);
  let remain = Math.ceil(seconds);
  loginSubmit.disabled = true;
  const tick = () => {
    if (remain <= 0) {
      clearInterval(lockdownTimer);
      loginSubmit.disabled = false;
      setError('');
      return;
    }
    setError(`尝试次数过多，请在 ${remain} 秒后再试`);
    remain -= 1;
  };
  tick();
  lockdownTimer = setInterval(tick, 1000);
}

loginForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const password = loginPassword.value;
  if (!password) {
    setError('请输入密码');
    return;
  }
  loginSubmit.disabled = true;
  setError('');
  try {
    const res = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password }),
    });
    if (res.ok) {
      window.location.href = '/';
      return;
    }
    const err = await res.json().catch(() => ({}));
    if (res.status === 429) {
      const match = /(\d+)\s*秒/.exec(err.detail || '');
      if (match) {
        startLockCountdown(parseInt(match[1], 10));
      } else {
        setError(err.detail || '请求过于频繁，请稍后再试');
        loginSubmit.disabled = false;
      }
    } else {
      setError(err.detail || '密码错误');
      loginSubmit.disabled = false;
    }
  } catch (err) {
    setError('网络错误，请重试');
    loginSubmit.disabled = false;
  }
  loginPassword.value = '';
  loginPassword.focus();
});
