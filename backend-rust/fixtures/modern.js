export default function () {
  const name = 'Rust Probe Modern';
  const React = window.SP_REACT;
  const api = window.__DECKY_SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED_deckyLoaderAPIInit.connect(1, name);
  function Content() {
    const [result, setResult] = React.useState(null);
    const [error, setError] = React.useState(null);
    React.useEffect(() => {
      let active = true;
      api.call('compute', [3, 4]).then(value => { if (active) setResult(value); }).catch(value => { if (active) setError(String(value)); });
      return () => { active = false; };
    }, []);
    return React.createElement('div', {style: {padding: '16px'}},
      React.createElement('h3', null, name + ' — validation fixture'),
      error ? React.createElement('p', null, 'Failure: ' + error) :
      result ? React.createElement('div', null,
        React.createElement('p', null, 'Rust → packaged CPython → Python plugin'),
        React.createElement('p', null, 'Computed result: ' + result.total),
        React.createElement('p', null, 'Python: ' + result.python + '; UID: ' + result.user),
        React.createElement('p', null, 'Native extensions: ' + result.native_modules.join(', ')),
        React.createElement('p', null, 'SDK compatibility alias: ' + result.alias)) : React.createElement('p', null, 'Calling the packaged worker…'));
  }
  return {name, content: React.createElement(Content), icon: React.createElement('span', null, 'R')};
}
