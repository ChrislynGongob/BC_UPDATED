(function () {
  const eyeIcon = '<svg viewBox="0 0 24 24" aria-hidden="true" style="display:block;width:20px;height:20px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12Z"></path><circle cx="12" cy="12" r="3"></circle><path class="eye-slash" d="m4 4 16 16"></path></svg>';

  document.querySelectorAll('input[type="password"]').forEach((input) => {
    const wrapper = document.createElement('span');
    wrapper.className = 'password-field-wrap';
    wrapper.style.cssText = 'position:relative;display:block;width:100%;';
    input.parentNode.insertBefore(wrapper, input);
    wrapper.appendChild(input);
    input.style.paddingRight = '48px';

    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'password-visibility-toggle';
    button.style.cssText = 'position:absolute;top:50%;right:12px;transform:translateY(-50%);display:grid;place-items:center;width:28px;height:28px;padding:0;border:0;background:transparent;color:#64748b;cursor:pointer;';
    button.setAttribute('aria-label', 'Show password');
    button.setAttribute('aria-pressed', 'false');
    button.innerHTML = eyeIcon;
    button.addEventListener('click', () => {
      const showPassword = input.type === 'password';
      input.type = showPassword ? 'text' : 'password';
      button.setAttribute('aria-pressed', String(showPassword));
      button.setAttribute('aria-label', showPassword ? 'Hide password' : 'Show password');
      input.focus();
    });
    wrapper.appendChild(button);
  });
})();
