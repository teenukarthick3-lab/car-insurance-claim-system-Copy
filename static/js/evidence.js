/* Car Insure — vehicle evidence capture, preview, AI photo check and submit. */
(function () {
  'use strict';

  var cfg = window.CLAIM_CONFIG || {};
  var form = document.getElementById('evidence-form');
  var csrf = form.querySelector('input[name=csrf_token]').value;

  var slots = Array.prototype.slice.call(document.querySelectorAll('.slot[data-key]'));
    var requiredSlots = slots.filter(function (s) { return s.dataset.optional !== '1'; });
  var custFields = {
    customer_name: document.getElementById('customer_name'),
    mobile: document.getElementById('mobile'),
    email: document.getElementById('email')
  };

  function customerErrors() {
    var errs = {};
    var name = custFields.customer_name.value.replace(/\s+/g, ' ').trim();
    var mobile = custFields.mobile.value.replace(/[\s\-()]/g, '');
    var email = custFields.email.value.trim();

    if (name && name.length < 2) {
        errs.customer_name = 'Please enter your full name.';
    }

    if (mobile) {
        var digits = mobile.indexOf('+91') === 0 ? mobile.slice(3) : mobile;
        if (digits.length === 11 && digits.charAt(0) === '0') {
            digits = digits.slice(1);
        }
        if (!/^[6-9]\d{9}$/.test(digits)) {
            errs.mobile = 'Enter a valid 10-digit mobile number.';
        }
    }

    if (email && !/^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$/.test(email)) {
        errs.email = 'Enter a valid email address.';
    }

    return errs;
}

  function showFieldErrors(errs) {
    Object.keys(custFields).forEach(function (k) {
      var box = document.getElementById('err-' + k);
      if (!box) return;
      box.textContent = errs[k] || '';
      box.classList.toggle('show', !!errs[k]);
    });
  }

  function missingItems() {
    var out = requiredSlots.filter(function (s) { return !isReady(s.dataset.key); }).map(labelFor);
    if (!(damagePhotos && damagePhotos.length)) out.push('Damage photo (at least one)');
    return out;
  }
  var submitBtn = document.getElementById('submit-btn');
  var submitLabel = document.getElementById('submit-label');
  var missingBox = document.getElementById('missing-box');
  var missingTitle = document.getElementById('missing-title');
  var missingText = document.getElementById('missing-text');
  var missingList = document.getElementById('missing-list');
  var overlay = document.getElementById('upload-overlay');
  var upBar = document.getElementById('up-bar');
  var upPct = document.getElementById('up-pct');
  var upTitle = document.getElementById('up-title');
  var upSub = document.getElementById('up-sub');

  var files = {};        // key -> File
  var urls = {};         // key -> object URL (revoked on replace/remove)
  var state = {};        // key -> 'checking' | 'ok' | 'bad' | 'error'
  var pending = {};      // key -> in-flight XHR for the AI photo check
  var submitting = false;

  var IMAGE_EXT = ['jpg', 'jpeg', 'png', 'webp'];
  var VIDEO_EXT = ['mp4', 'mov'];
  var IMAGE_MIME = ['image/jpeg', 'image/png', 'image/webp'];
  var VIDEO_MIME = ['video/mp4', 'video/quicktime'];

  function ext(name) {
    var i = (name || '').lastIndexOf('.');
    return i >= 0 ? name.slice(i + 1).toLowerCase() : '';
  }

  function fmtSize(bytes) {
    return bytes >= 1048576 ? (bytes / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(bytes / 1024)) + ' KB';
  }

  function slotByKey(key) {
    return slots.filter(function (s) { return s.dataset.key === key; })[0];
  }

  function check(kind, file) {
    var e = ext(file.name);
    var type = (file.type || '').toLowerCase();
    if (kind === 'image') {
      if (type === 'image/heic' || type === 'image/heif' || e === 'heic' || e === 'heif') {
        return 'HEIC photos aren’t supported. Use “Add photo”, or pick a JPG/PNG image.';
      }
      var okImg = IMAGE_MIME.indexOf(type) >= 0 || (!type && IMAGE_EXT.indexOf(e) >= 0);
      if (!okImg) return 'Please choose a JPG, PNG or WEBP photo.';
      if (file.size > cfg.maxImageMB * 1048576) return 'This photo is larger than ' + cfg.maxImageMB + ' MB. Please retake it.';
    } else {
      var okVid = VIDEO_MIME.indexOf(type) >= 0 || ((!type || type === 'video/*') && VIDEO_EXT.indexOf(e) >= 0);
      if (!okVid) return 'Please choose an MP4 or MOV video.';
      if (file.size > cfg.maxVideoMB * 1048576) return 'This video is larger than ' + cfg.maxVideoMB + ' MB. Record a shorter clip (about 30–60 seconds).';
    }
    if (file.size === 0) return 'This file is empty. Please try again.';
    return null;
  }

  function isReady(key) { return !!files[key] && state[key] === 'ok'; }

  function setZone(key, on) {
    var z = document.querySelector('.zone[data-zone="' + key + '"]');
    if (z) z.classList.toggle('on', on);
  }

  function labelFor(slot) {
    var key = slot.dataset.key;
    var base = slot.dataset.label;
    if (!files[key]) return base;
    if (state[key] === 'checking') return base + ' (still being checked)';
    if (state[key] === 'bad') return base + ' (photo not accepted, please replace)';
    if (state[key] === 'error') return base + ' (not checked yet, tap Try again)';
    return base;
  }

    function updateProgress() {
    var readyCount = requiredSlots.filter(function (s) { return isReady(s.dataset.key); }).length;
    var hasDamage = !!(damagePhotos && damagePhotos.length);
    var total = requiredSlots.length + 1;
    var done = readyCount + (hasDamage ? 1 : 0);
    var custOk = Object.keys(customerErrors()).length === 0;
    document.getElementById('done-count').textContent = done;
    var totalEl = document.getElementById('total-count');
    if (totalEl) totalEl.textContent = total;
    document.getElementById('meter').style.width = (done / total * 100) + '%';
    var ready = done === total && custOk;
    submitBtn.classList.toggle('not-ready', !ready);
    submitLabel.textContent = ready ? 'Submit claim' : 'Submit claim (' + done + '/' + total + ')';
    slots.forEach(function (s) {
      var key = s.dataset.key;
      var li = document.querySelector('[data-check="' + key + '"]');
      if (li) li.classList.toggle('on', isReady(key));
      setZone(key, isReady(key));
    });
    var dli = document.querySelector('[data-check="damage_photos"]');
    if (dli) dli.classList.toggle('on', hasDamage);
    var cli = document.querySelector('[data-check="customer"]');
    if (cli) cli.classList.toggle('on', custOk);
    if (!missingBox.hidden && missingList.children.length) {
      var still = missingItems();
      if (!still.length) missingBox.hidden = true;
      else showMissing(still, true);
    }
  }

  function showSlotError(slot, msg) {
    var el = slot.querySelector('[data-role="error"]');
    el.textContent = msg || '';
    el.classList.toggle('show', !!msg);
  }

  /* ---- AI check presentation ---- */
  function setState(slot, s, title, message) {
    var key = slot.dataset.key;
    state[key] = s;
    slot.setAttribute('data-state', s);
    var text = slot.querySelector('[data-role="state-text"]');
    var box = slot.querySelector('[data-role="ai-status"]');
    var retry = slot.querySelector('[data-action="retry"]');
    var chip = slot.querySelector('.ai-chip');
    if (s === 'checking') text.textContent = key === 'chassis_photo' ? 'Reading chassis number…' : 'Analyzing photo…';
    else if (s === 'ok') text.textContent = '✓ ' + (title || 'Photo added');
    else if (s === 'bad') text.textContent = '✕ Photo not accepted';
    else if (s === 'error') text.textContent = '! Not checked';
    if (chip && s === 'ok') chip.textContent = '✓ ' + (title || 'Verified');
    if (box) {
      var show = s === 'bad' || s === 'error';
      box.hidden = !show;
      if (show) {
        slot.querySelector('[data-role="ai-title"]').textContent = title || '';
        slot.querySelector('[data-role="ai-message"]').textContent = message || '';
      }
    }
    if (retry) retry.hidden = s !== 'error';
    if (s === 'ok') slot.classList.remove('missing');
    updateProgress();
  }

  function showChassis(slot, info) {
    var box = slot.querySelector('[data-role="chassis"]');
    if (!box) return;
    box.hidden = false;
    box.classList.toggle('uncertain', !info.confident);
    slot.querySelector('[data-role="chassis-value"]').textContent = info.number || '—';
    slot.querySelector('[data-role="chassis-note"]').textContent = info.confident ? '' : info.message;
  }

  function hideChassis(slot) {
    var box = slot.querySelector('[data-role="chassis"]');
    if (box) box.hidden = true;
  }

  function validatePhoto(slot) {
    var key = slot.dataset.key;
    var file = files[key];
    if (!file) return;
    if (pending[key]) pending[key].abort();
    hideChassis(slot);
    setState(slot, 'checking');

    var data = new FormData();
    data.append('csrf_token', csrf);
    data.append('slot', key);
    data.append('photo', file, file.name);

    var xhr = new XMLHttpRequest();
    pending[key] = xhr;
    xhr.open('POST', cfg.validateUrl);
    xhr.setRequestHeader('X-Requested-With', 'fetch');
    xhr.timeout = 90000;
    xhr.onload = function () {
      if (pending[key] !== xhr || files[key] !== file) return;   // replaced meanwhile
      delete pending[key];
      var res = null;
      try { res = JSON.parse(xhr.responseText); } catch (e) { /* non-JSON */ }
      if (xhr.status === 401) {
        setState(slot, 'error', 'Session expired', (res && res.error) || 'Please verify your policy number again.');
        setTimeout(function () { window.location.assign('/claim'); }, 2500);
        return;
      }
      if (!res || !res.ok) {
        setState(slot, 'error', 'We couldn’t check this photo', (res && res.error) || 'Please try again in a moment.');
        return;
      }
      if (res.valid) {
          setState(
              slot,
              'ok',
              res.chassis && res.chassis.confident
                  ? 'Chassis number detected'
                  : res.title,
              res.message
          );

          if (res.chassis) {
              showChassis(slot, res.chassis);
          }

          // Display the registration number detected by Gemini.
          if (
              res.registration &&
              res.registration.number &&
              (key === 'front_photo' || key === 'back_photo')
          ) {
              var registrationInput =
                  document.getElementById('vehicle-number-input');

              if (registrationInput) {
                  registrationInput.value = res.registration.number;
              }
          }
      }
      else if (res.retryable) setState(slot, 'error', res.title, res.message);
      else setState(slot, 'bad', res.title, res.message);
    };
    function failed(msg) {
      return function () {
        if (pending[key] !== xhr) return;
        delete pending[key];
        setState(slot, 'error', 'We couldn’t check this photo', msg);
      };
    }
    xhr.onerror = failed('Connection problem. Check you are on the same Wi-Fi and tap Try again.');
    xhr.ontimeout = failed('The check took too long. Please tap Try again.');
    xhr.send(data);
  }

  var picks = {};   // key -> latest pick id (ignore slow reads of an older pick)

  function setFile(slot, file) {
    var key = slot.dataset.key;
    var kind = slot.dataset.kind;
    var err = check(kind, file);
    if (err) { showSlotError(slot, err); return; }
    showSlotError(slot, '');
    if (kind !== 'image' || !file.arrayBuffer) { useFile(slot, file); return; }

    // Copy the photo into memory first. Some phones (Android photo picker, cloud galleries)
    // only allow the original file to be read briefly, which breaks the preview and uploads.
    var pick = picks[key] = (picks[key] || 0) + 1;
    file.arrayBuffer().then(function (buf) {
      if (picks[key] !== pick) return;
      var copy;
      try { copy = new File([buf], file.name, { type: file.type || 'image/jpeg', lastModified: file.lastModified }); }
      catch (e) { copy = new Blob([buf], { type: file.type || 'image/jpeg' }); copy.name = file.name; }
      useFile(slot, copy);
    }, function () {
      if (picks[key] !== pick) return;
      showSlotError(slot, 'We couldn’t read this photo from your phone. Please choose it again, or use “Add photo” to take it with the camera.');
    });
  }

  function useFile(slot, file) {
    var key = slot.dataset.key;
    var kind = slot.dataset.kind;

    if (pending[key]) { pending[key].abort(); delete pending[key]; }
    if (urls[key]) URL.revokeObjectURL(urls[key]);
    files[key] = file;
    urls[key] = URL.createObjectURL(file);

    var media = slot.querySelector('[data-role="media"]');
    media.innerHTML = '';
    var el;
    if (kind === 'image') {
      el = document.createElement('img');
      el.alt = '';
      el.onerror = function () {
        media.innerHTML = '<div class="no-preview">Preview not available on this phone.<br>Your photo was still received.</div>';
      };
    } else {
      el = document.createElement('video');
      el.controls = true;
      el.muted = true;
      el.setAttribute('playsinline', '');
      el.preload = 'metadata';
    }
    el.src = urls[key];
    media.appendChild(el);
    slot.querySelector('[data-role="fname"]').textContent = file.name + ' · ' + fmtSize(file.size);
    slot.classList.add('done');
    slot.classList.remove('missing');

    if (kind === 'image') validatePhoto(slot);
    else setState(slot, 'ok', 'Video added');
  }

  function clearFile(slot) {
    var key = slot.dataset.key;
    if (pending[key]) { pending[key].abort(); delete pending[key]; }
    if (urls[key]) URL.revokeObjectURL(urls[key]);
    delete files[key];
    delete urls[key];
    delete state[key];
    slot.removeAttribute('data-state');
    hideChassis(slot);
    var box = slot.querySelector('[data-role="ai-status"]');
    if (box) box.hidden = true;
    slot.querySelector('[data-role="media"]').innerHTML = '';
    slot.classList.remove('done');
    slot.querySelectorAll('input[type=file]').forEach(function (i) { i.value = ''; });
    updateProgress();
  }

  slots.forEach(function (slot) {
    slot.querySelectorAll('input[type=file]').forEach(function (input) {
      // Reset when the picker opens (so the same file can be chosen again). Resetting right
      // after picking can revoke access to photos from Android's photo picker.
      input.addEventListener('click', function () { input.value = ''; });
      input.addEventListener('change', function () {
        if (input.files && input.files[0]) setFile(slot, input.files[0]);
      });
    });
    slot.querySelector('[data-action="remove"]').addEventListener('click', function () { clearFile(slot); });
    slot.querySelector('[data-action="replace"]').addEventListener('click', function () {
      // The library picker also offers the camera on iOS and Android.
      slot.querySelector('input[data-role="library"]').click();
    });
    var retry = slot.querySelector('[data-action="retry"]');
    if (retry) retry.addEventListener('click', function () { validatePhoto(slot); });
  });

  /* ---- Damage Photos manager ---- */
  var damagePhotos = []; // array of { file: File, url: string }
  var dmgCamInput = document.getElementById('damage-photo-camera');
  var dmgLibInput = document.getElementById('damage-photos-library');
  var dmgPreviews = document.getElementById('damage-previews');
  var dmgEmpty = document.getElementById('damage-photos-empty');
  var dmgError = document.getElementById('damage-photos-error');

  function showDmgError(msg) {
    if (!dmgError) return;
    dmgError.textContent = msg || '';
    dmgError.classList.toggle('show', !!msg);
  }

  function renderDamagePreviews() {
    if (!dmgPreviews) return;
    dmgPreviews.innerHTML = '';
    if (dmgEmpty) dmgEmpty.style.display = damagePhotos.length ? 'none' : 'block';
    updateProgress();

    damagePhotos.forEach(function (dp, idx) {
      var card = document.createElement('div');
      card.className = 'damage-thumb-card';
      card.style.cssText = 'background:#fff; border:1px solid var(--line); border-radius:10px; overflow:hidden; box-shadow:0 2px 8px rgba(0,0,0,0.06); display:flex; flex-direction:column;';

      var imgWrap = document.createElement('div');
      imgWrap.style.cssText = 'position:relative; background:#0B0F14; aspect-ratio:4/3; overflow:hidden;';
      var img = document.createElement('img');
      img.src = dp.url;
      img.alt = 'Damage photo ' + (idx + 1);
      img.style.cssText = 'width:100%; height:100%; object-fit:cover; display:block;';
      imgWrap.appendChild(img);

      var tag = document.createElement('span');
      tag.textContent = '#' + (idx + 1);
      tag.style.cssText = 'position:absolute; top:6px; left:6px; background:rgba(11,15,20,0.8); color:var(--gold); font-size:11px; font-weight:800; padding:2px 6px; border-radius:4px;';
      imgWrap.appendChild(tag);
      card.appendChild(imgWrap);

      var meta = document.createElement('div');
      meta.style.cssText = 'padding:6px 8px; font-size:11px; color:var(--muted); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; border-bottom:1px solid var(--line);';
      meta.textContent = dp.file.name + ' · ' + fmtSize(dp.file.size);
      card.appendChild(meta);

      var btns = document.createElement('div');
      btns.style.cssText = 'display:flex; gap:4px; padding:6px 8px; background:var(--light);';

      var replaceBtn = document.createElement('button');
      replaceBtn.type = 'button';
      replaceBtn.className = 'mini-btn';
      replaceBtn.style.cssText = 'flex:1; padding:4px 0; font-size:11px;';
      replaceBtn.textContent = 'Replace';

      var repInput = document.createElement('input');
      repInput.type = 'file';
      repInput.className = 'visually-hidden';
      repInput.accept = 'image/jpeg,image/png,image/webp,.jpg,.jpeg,.png,.webp';
      repInput.addEventListener('change', function () {
        if (repInput.files && repInput.files[0]) {
          var f = repInput.files[0];
          var err = check('image', f);
          if (err) { showDmgError(err); return; }
          showDmgError('');
          URL.revokeObjectURL(dp.url);
          dp.file = f;
          dp.url = URL.createObjectURL(f);
          renderDamagePreviews();
        }
      });
      replaceBtn.addEventListener('click', function () { repInput.click(); });

      var removeBtn = document.createElement('button');
      removeBtn.type = 'button';
      removeBtn.className = 'mini-btn remove';
      removeBtn.style.cssText = 'flex:1; padding:4px 0; font-size:11px;';
      removeBtn.textContent = 'Remove';
      removeBtn.addEventListener('click', function () {
        URL.revokeObjectURL(dp.url);
        damagePhotos.splice(idx, 1);
        renderDamagePreviews();
      });

      btns.appendChild(replaceBtn);
      btns.appendChild(repInput);
      btns.appendChild(removeBtn);
      card.appendChild(btns);

      dmgPreviews.appendChild(card);
    });
  }

  function addDamageFiles(fileList) {
    if (!fileList || !fileList.length) return;
    showDmgError('');
    var added = 0;
    Array.prototype.slice.call(fileList).forEach(function (f) {
      var err = check('image', f);
      if (err) {
        showDmgError(f.name + ': ' + err);
        return;
      }
      damagePhotos.push({ file: f, url: URL.createObjectURL(f) });
      added++;
    });
    if (added) renderDamagePreviews();
  }

  if (dmgCamInput) {
    dmgCamInput.addEventListener('click', function () { dmgCamInput.value = ''; });
    dmgCamInput.addEventListener('change', function () {
      if (dmgCamInput.files && dmgCamInput.files.length) addDamageFiles(dmgCamInput.files);
    });
  }
  if (dmgLibInput) {
    dmgLibInput.addEventListener('click', function () { dmgLibInput.value = ''; });
    dmgLibInput.addEventListener('change', function () {
      if (dmgLibInput.files && dmgLibInput.files.length) addDamageFiles(dmgLibInput.files);
    });
  }

  function showMissing(labels, quiet) {
    missingTitle.textContent = 'Please complete your vehicle evidence.';
    missingText.textContent = 'Missing:';
    missingList.innerHTML = '';
    labels.forEach(function (l) {
      var li = document.createElement('li');
      li.textContent = l;
      missingList.appendChild(li);
    });
    missingBox.hidden = false;
    if (quiet) return;
    missingBox.classList.remove('shake');
    void missingBox.offsetWidth;
    missingBox.classList.add('shake');
  }

  function showServerError(msg) {
    missingTitle.textContent = 'We couldn’t submit your claim';
    missingText.textContent = msg;
    missingList.innerHTML = '';
    missingBox.hidden = false;
  }

  function setBusy(busy) {
    submitting = busy;
    submitBtn.disabled = busy;
    submitBtn.classList.toggle('loading', busy);
    overlay.classList.toggle('show', busy);
    if (busy) {
      upBar.style.width = '0%';
      upPct.textContent = '0%';
      upTitle.textContent = 'Submitting your claim';
      upSub.textContent = 'Uploading your photos and video. Keep this page open.';
    }
  }

  Object.keys(custFields).forEach(function (k) {
    custFields[k].addEventListener('input', function () {
      var box = document.getElementById('err-' + k);
      if (box) { box.textContent = ''; box.classList.remove('show'); }
      updateProgress();
    });
  });

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    if (submitting) return;

    var custErrs = customerErrors();
    var errKeys = Object.keys(custErrs);
    if (errKeys.length) {
      showFieldErrors(custErrs);
      custFields[errKeys[0]].focus();
      custFields[errKeys[0]].scrollIntoView({ behavior: 'smooth', block: 'center' });
      return;
    }
    showFieldErrors({});

    var notReady = requiredSlots.filter(function (s) { return !isReady(s.dataset.key); });
    var noDamage = damagePhotos.length === 0;
    if (notReady.length || noDamage) {
      notReady.forEach(function (s) { if (state[s.dataset.key] !== 'checking') s.classList.add('missing'); });
      var checking = notReady.filter(function (s) { return state[s.dataset.key] === 'checking'; });
      showMissing(missingItems());
      if (noDamage) showDmgError('Please add at least one damage photo.');
      if (notReady.length && checking.length === notReady.length && !noDamage) {
        missingTitle.textContent = 'Please wait a moment.';
        missingText.textContent = 'We’re still checking:';
      }
      var target = notReady.length ? notReady[0] : document.getElementById('damage-photos-slot');
      target.scrollIntoView({ behavior: 'smooth', block: 'center' });
      return;
    }
    missingBox.hidden = true;

    var data = new FormData();
    data.append('csrf_token', csrf);
    data.append('customer_name', custFields.customer_name.value.trim());
    data.append('mobile', custFields.mobile.value.trim());
    data.append('email', custFields.email.value.trim());
    Object.keys(files).forEach(function (k) { data.append(k, files[k], files[k].name); });
    damagePhotos.forEach(function (dp) {
      data.append('damage_photos', dp.file, dp.file.name);
    });

    setBusy(true);
    var xhr = new XMLHttpRequest();
    xhr.open('POST', cfg.submitUrl);
    xhr.setRequestHeader('X-Requested-With', 'fetch');
    xhr.upload.onprogress = function (ev) {
      if (!ev.lengthComputable) return;
      var pct = Math.min(99, Math.round(ev.loaded / ev.total * 100));
      upBar.style.width = pct + '%';
      upPct.textContent = pct + '%';
      if (pct >= 99) { upTitle.textContent = 'Almost done'; upSub.textContent = 'Checking your photos and creating your claim reference…'; }
    };
    xhr.onload = function () {
      var res = null;
      try { res = JSON.parse(xhr.responseText); } catch (err) { /* non-JSON */ }
      if (xhr.status >= 200 && xhr.status < 300 && res && res.ok) {
        upBar.style.width = '100%';
        upPct.textContent = '100%';
        window.location.assign(res.redirect);
        return;
      }
      setBusy(false);
      if (res && res.errors && Object.keys(res.errors).length) {
        showFieldErrors(res.errors);
        var fk = Object.keys(res.errors)[0];
        if (custFields[fk]) {
          custFields[fk].focus();
          custFields[fk].scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
        return;
      }
      if (xhr.status === 401) {
        showServerError((res && res.error) || 'Something went wrong. Please try again.');
        return;
      }
      var rejected = (res && res.rejected) || [];
      rejected.forEach(function (r) {
        var slot = slotByKey(r.slot);
        if (slot) setState(slot, r.retryable ? 'error' : 'bad', r.title, r.message);
      });
      showServerError((res && res.error) || 'Something went wrong. Please try again.');
      if (rejected.length) {
        var first = slotByKey(rejected[0].slot);
        if (first) first.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
    };
    xhr.onerror = function () {
      setBusy(false);
      showServerError('Connection lost while uploading. Check you are on the same Wi-Fi and try again.');
    };
    xhr.send(data);
  });

  window.addEventListener('beforeunload', function (e) {
    if (submitting) { e.preventDefault(); e.returnValue = ''; }


  });

  updateProgress();
})();
