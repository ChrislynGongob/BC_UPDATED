(() => {
  const button = document.getElementById('resend-otp');
  const status = document.getElementById('resend-status');
  const deadline = Date.now() + Number(button.dataset.wait) * 1000;
  const update = () => {
    const seconds = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
    button.disabled = seconds > 0;
    status.textContent = seconds ? `Resend available in ${seconds} seconds.` : 'You can request another code.';
    if (!seconds) clearInterval(timer);
  };
  const timer = setInterval(update, 1000);
  update();
})();
