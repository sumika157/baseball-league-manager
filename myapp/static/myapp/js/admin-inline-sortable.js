/*
 * 管理画面のインライン行をドラッグで並べ替える。
 *
 * 外部ライブラリは使わず、HTML5 のドラッグ＆ドロップだけで実装する。
 * 並べ替えた結果は各行の「表示順」入力欄に 0, 1, 2 … と振り直すだけで、
 * 保存は通常の「保存」ボタンで行う（非同期の保存はしない）。
 *
 * 対象は name が "-display_order" で終わる入力欄を持つインライン行。
 * インライン側にマークアップを足さずに済むよう、入力欄の名前で見つける。
 */
(function () {
  'use strict';

  var ORDER_SELECTOR = 'input[name$="-display_order"]';

  function tablesWithOrderField() {
    var tables = [];
    document.querySelectorAll(ORDER_SELECTOR).forEach(function (input) {
      var table = input.closest('table');
      if (table && tables.indexOf(table) === -1) tables.push(table);
    });
    return tables;
  }

  function init(table) {
    var tbody = table.querySelector('tbody');
    if (!tbody) return;

    var dragged = null;

    function rows() {
      // 「追加」用の空行は対象外
      return Array.prototype.filter.call(tbody.querySelectorAll('tr'), function (tr) {
        return !tr.classList.contains('empty-form') && tr.querySelector(ORDER_SELECTOR);
      });
    }

    function renumber() {
      rows().forEach(function (tr, index) {
        tr.querySelector(ORDER_SELECTOR).value = index;
      });
    }

    function clearHighlight() {
      rows().forEach(function (r) { r.classList.remove('is-drop-target'); });
    }

    function decorate(tr) {
      if (tr.dataset.sortableReady) return;
      tr.dataset.sortableReady = '1';
      tr.setAttribute('draggable', 'true');
      tr.classList.add('sortable-row');

      var firstCell = tr.querySelector('td');
      if (firstCell && !firstCell.querySelector('.sortable-handle')) {
        var handle = document.createElement('span');
        handle.className = 'sortable-handle';
        handle.title = 'ドラッグして並べ替え';
        handle.textContent = '⠿';
        firstCell.insertBefore(handle, firstCell.firstChild);
      }

      tr.addEventListener('dragstart', function (event) {
        dragged = tr;
        tr.classList.add('is-dragging');
        event.dataTransfer.effectAllowed = 'move';
        // Firefox はデータを入れないとドラッグが始まらない
        event.dataTransfer.setData('text/plain', '');
      });

      tr.addEventListener('dragend', function () {
        tr.classList.remove('is-dragging');
        clearHighlight();
        dragged = null;
        renumber();
      });

      tr.addEventListener('dragover', function (event) {
        if (!dragged || dragged === tr) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = 'move';
        tr.classList.add('is-drop-target');
      });

      tr.addEventListener('dragleave', function () {
        tr.classList.remove('is-drop-target');
      });

      tr.addEventListener('drop', function (event) {
        if (!dragged || dragged === tr) return;
        event.preventDefault();
        tr.classList.remove('is-drop-target');

        var list = rows();
        var from = list.indexOf(dragged);
        var to = list.indexOf(tr);
        if (from < 0 || to < 0) return;

        if (from < to) {
          tr.parentNode.insertBefore(dragged, tr.nextSibling);
        } else {
          tr.parentNode.insertBefore(dragged, tr);
        }
        renumber();
      });
    }

    rows().forEach(decorate);

    // 「もう1つ追加」で行が増えたときにも適用する
    new MutationObserver(function () {
      rows().forEach(decorate);
    }).observe(tbody, { childList: true });
  }

  document.addEventListener('DOMContentLoaded', function () {
    tablesWithOrderField().forEach(init);
  });
})();
