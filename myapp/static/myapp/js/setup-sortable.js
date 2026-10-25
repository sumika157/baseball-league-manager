/*
 * 登録画面の一覧（リーグ・チーム）の行をドラッグで並べ替える（W3 T8。#79）。
 *
 * 管理画面の admin-inline-sortable.js と同じ考え方で、外部ライブラリは使わず HTML5 のドラッグ＆ドロップだけで
 * 実装する。並べ替えた結果は、一覧ごとのフォーム（tbody の data-sortable-form が id で指す）の隠し欄 order に
 * 「識別子の並び」で書き直すだけで、保存はそのフォームの送信で行う（非同期の保存はしない）。
 * サーバは送られた識別子の集まりが今の行とちょうど一致するときだけ書く（位置ではなく識別子で組み立てる）。
 *
 * JS が動かなければ、保存のフォームと案内は hidden のままで、一覧はそのまま読める（並べ替えができないだけ）。
 * チームの一覧はリーグごとに別の tbody とフォームなので、リーグをまたいでは動かせない。
 */
(function () {
  'use strict';

  function init(tbody) {
    var form = document.getElementById(tbody.dataset.sortableForm);
    if (!form) return;

    function rows() {
      return Array.prototype.slice.call(tbody.querySelectorAll('tr[data-order-id]'));
    }

    function currentIds() {
      return rows().map(function (tr) { return tr.dataset.orderId; });
    }

    // 1行だけなら並べ替えるものが無い
    if (rows().length < 2) return;

    var initial = currentIds().join(',');
    var dragged = null;
    var hint = tbody.dataset.sortableHint && document.getElementById(tbody.dataset.sortableHint);
    if (hint) hint.hidden = false;

    function sync() {
      // 隠し欄を今の行の並びで作り直す。並びが開いたときと同じなら、保存のフォームを隠す
      form.querySelectorAll('input[name="order"]').forEach(function (input) { input.remove(); });
      var fields = document.createDocumentFragment();
      currentIds().forEach(function (id) {
        var input = document.createElement('input');
        input.type = 'hidden';
        input.name = 'order';
        input.value = id;
        fields.appendChild(input);
      });
      form.insertBefore(fields, form.firstChild);
      form.hidden = currentIds().join(',') === initial;
    }

    function clearHighlight() {
      rows().forEach(function (r) { r.classList.remove('is-drop-target'); });
    }

    rows().forEach(function (tr) {
      tr.setAttribute('draggable', 'true');
      tr.classList.add('sortable-row');

      // つまみは名前の欄（data-sortable-handle）の先頭に置く
      var target = tr.querySelector('[data-sortable-handle]');
      if (target) {
        var handle = document.createElement('span');
        handle.className = 'sortable-handle';
        handle.title = 'ドラッグして並べ替え';
        handle.setAttribute('aria-hidden', 'true');
        handle.textContent = '⠿';
        target.insertBefore(handle, target.firstChild);
      }

      tr.addEventListener('dragstart', function (event) {
        dragged = tr;
        tr.classList.add('is-dragging');
        event.dataTransfer.effectAllowed = 'move';
        // Firefox はデータを入れないとドラッグが始まらない
        event.dataTransfer.setData('text/plain', tr.dataset.orderId);
      });

      tr.addEventListener('dragend', function () {
        tr.classList.remove('is-dragging');
        clearHighlight();
        dragged = null;
        sync();
      });

      tr.addEventListener('dragover', function (event) {
        // ほかの一覧（ほかのリーグ）の行は受け付けない
        if (!dragged || dragged === tr || dragged.parentNode !== tbody) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = 'move';
        tr.classList.add('is-drop-target');
      });

      tr.addEventListener('dragleave', function () {
        tr.classList.remove('is-drop-target');
      });

      tr.addEventListener('drop', function (event) {
        if (!dragged || dragged === tr || dragged.parentNode !== tbody) return;
        event.preventDefault();
        tr.classList.remove('is-drop-target');
        var list = rows();
        var from = list.indexOf(dragged);
        var to = list.indexOf(tr);
        if (from < 0 || to < 0) return;
        tbody.insertBefore(dragged, from < to ? tr.nextSibling : tr);
        sync();
      });
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('tbody[data-sortable-form]').forEach(init);
  });
})();
