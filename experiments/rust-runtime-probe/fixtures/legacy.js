(function () {
  return function (serverAPI) {
    const React = window.SP_REACT;
    function Content() {
      const [result, setResult] = React.useState(null);
      const [error, setError] = React.useState(null);
      React.useEffect(() => {
        let active = true;
        serverAPI.callPluginMethod('compute', {values: [3, 4]}).then(response => {
          if (!response.success) throw new Error(String(response.result));
          if (active) setResult(response.result);
        }).catch(value => { if (active) setError(String(value)); });
        return () => { active = false; };
      }, []);
      return React.createElement('div', {style: {padding: '16px'}},
        React.createElement('h3', null, 'Rust Probe Legacy — validation fixture'),
        error ? React.createElement('p', null, 'Failure: ' + error) :
        result ? React.createElement('div', null,
          React.createElement('p', null, 'Legacy keyword arguments and class/self API'),
          React.createElement('p', null, 'Computed result: ' + result.total),
          React.createElement('p', null, 'Python: ' + result.python + '; UID: ' + result.user)) : React.createElement('p', null, 'Calling the packaged worker…'));
    }
    return {name: 'Rust Probe Legacy', content: React.createElement(Content), icon: React.createElement('span', null, 'L')};
  };
})()
