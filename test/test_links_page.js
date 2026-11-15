// Link page hide/show checks: npm install jsdom, then node test/test_links_page.js
const fs = require('fs');
const { JSDOM } = require('jsdom');
const html = fs.readFileSync(process.argv[2] || __dirname + '/../site/index.html', 'utf8');
const posts = [];
let fails = 0;
const check = (n, c) => { console.log((c ? 'PASS  ' : 'FAIL  ') + n); if (!c) fails++; };
const dom = new JSDOM(html, { runScripts: 'dangerously', url: 'https://x.test/links/',
  beforeParse(w) {
    w.fetch = (url, opts) => {
      if (opts && opts.method === 'POST') { posts.push(JSON.parse(opts.body)); return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) }); }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ hidden: ['meal-planner', 'gone-tool'] }) });
    };
  } });
const d = dom.window.document;
const ids = sel => Array.from(d.querySelectorAll(sel + ' li')).map(li => li.dataset.id);
setTimeout(() => {
  check('no PC-only section', !d.body.textContent.includes('Only on the PC'));
  check('meal planner moved to Hidden', ids('#hidden-list').join() === 'meal-planner');
  check('Hidden box shown with count', !d.getElementById('hidden-box').hidden && d.getElementById('hidden-summary').textContent === 'Hidden (1)');
  check('toggles invisible until Edit', !d.body.classList.contains('editing'));
  d.getElementById('edit').click();
  check('Edit turns on editing', d.body.classList.contains('editing') && d.getElementById('edit').textContent === 'Done');
  d.querySelector('li[data-id="crossbuilder"] button.toggle').click();
  check('hide crossbuilder saves both, unknown id dropped', JSON.stringify(posts[0]) === JSON.stringify({ hidden: ['meal-planner', 'crossbuilder'] }));
  check('hidden list in page order', ids('#hidden-list').join() === 'crossbuilder,meal-planner');
  d.querySelector('li[data-id="meal-planner"] button.toggle').click();
  check('show puts meal planner back in place', ids('section:nth-of-type(2)').join() === 'rss-filter,unseen,meal-planner');
  check('show saves', JSON.stringify(posts[1]) === JSON.stringify({ hidden: ['crossbuilder'] }));
  ['rss-filter', 'unseen', 'meal-planner'].forEach(id => d.querySelector(`li[data-id="${id}"] button.toggle`).click());
  check('empty section heading hidden', d.querySelectorAll('section')[1].classList.contains('empty'));
  d.querySelector('li[data-id="crossbuilder"] button.toggle').click();
  check('crossbuilder back second in its section', ids('section:nth-of-type(1)')[1] === 'crossbuilder');
  check('toggle buttons labelled', d.querySelector('li[data-id="unseen"] button.toggle').getAttribute('aria-label') === 'Show Unseen');
  console.log(fails ? fails + ' FAILED' : 'all checks passed');
  process.exit(fails ? 1 : 0);
}, 50);
