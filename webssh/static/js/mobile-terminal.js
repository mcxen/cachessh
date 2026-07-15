/*jslint browser:true */

(function (window, document) {
  'use strict';

  var shortcuts = [
    {label: 'Ctrl+C', data: '\u0003', title: '中断当前命令'},
    {label: 'Ctrl+L', data: '\u000c', title: '清空终端'},
    {label: 'Ctrl+D', data: '\u0004', title: '结束输入或退出'},
    {label: 'Tab', data: '\t', title: '自动补全'},
    {label: 'Esc', data: '\u001b', title: 'Escape'},
    {label: '\u2191', data: '\u001b[A', title: '向上'},
    {label: '\u2193', data: '\u001b[B', title: '向下'},
    {label: '\u2190', data: '\u001b[D', title: '向左'},
    {label: '\u2192', data: '\u001b[C', title: '向右'},
    {label: 'Enter', data: '\r', title: '回车'}
  ];

  function isMobileDevice() {
    var coarsePointer = window.matchMedia &&
      window.matchMedia('(pointer: coarse)').matches;
    var narrowViewport = window.matchMedia ?
      window.matchMedia('(max-width: 767.98px)').matches : window.innerWidth <= 768;
    var mobileAgent = /Android|webOS|iPhone|iPad|iPod|IEMobile|Opera Mini/i
      .test(window.navigator.userAgent || '');
    var touchViewport = window.navigator.maxTouchPoints > 0 &&
      Math.min(window.screen.width, window.screen.height) <= 1024;

    return Boolean(narrowViewport || coarsePointer || mobileAgent || touchViewport);
  }

  function createShortcutBar() {
    var bar = document.createElement('div');
    bar.className = 'mobile-shortcuts';
    bar.setAttribute('role', 'toolbar');
    bar.setAttribute('aria-label', '\u7ec8\u7aef\u5feb\u6377\u6307\u4ee4');

    shortcuts.forEach(function (shortcut) {
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'mobile-shortcut';
      button.textContent = shortcut.label;
      button.title = shortcut.title;
      button.setAttribute('aria-label', shortcut.title + ' (' + shortcut.label + ')');
      button.addEventListener('pointerdown', function (event) {
        event.preventDefault();
      });
      button.addEventListener('click', function () {
        if (bar.send) {
          bar.send(shortcut.data);
        }
        if (bar.terminal) {
          bar.terminal.focus();
        }
      });
      bar.appendChild(button);
    });

    document.body.appendChild(bar);
    return bar;
  }

  function init(options) {
    var bar, resizeTimer;

    options = options || {};
    if (!isMobileDevice()) {
      return null;
    }

    document.body.classList.add('mobile-device');
    document.body.classList.add('mobile-terminal-active');
    bar = document.querySelector('.mobile-shortcuts') || createShortcutBar();
    bar.send = options.send;
    bar.terminal = options.terminal;

    function handleViewportResize() {
      window.clearTimeout(resizeTimer);
      resizeTimer = window.setTimeout(function () {
        if (options.onResize) {
          options.onResize();
        }
      }, 80);
    }

    if (window.visualViewport) {
      window.visualViewport.addEventListener('resize', handleViewportResize);
    }

    bar.destroy = function () {
      window.clearTimeout(resizeTimer);
      if (window.visualViewport) {
        window.visualViewport.removeEventListener('resize', handleViewportResize);
      }
      bar.send = null;
      bar.terminal = null;
      if (bar.parentNode) {
        bar.parentNode.removeChild(bar);
      }
      document.body.classList.remove('mobile-terminal-active');
      document.body.classList.remove('mobile-device');
    };

    handleViewportResize();
    return bar;
  }

  window.MobileTerminalControls = {
    init: init,
    isMobileDevice: isMobileDevice
  };
}(window, document));
