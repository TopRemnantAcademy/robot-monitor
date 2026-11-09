import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import { I18nProvider } from './i18n/I18nProvider';
import './index.css';

const container = document.getElementById('root');
if (!container) {
  throw new Error('#root container missing: index.html must contain <div id="root">');
}

ReactDOM.createRoot(container).render(
  <React.StrictMode>
    <I18nProvider>
      <App />
    </I18nProvider>
  </React.StrictMode>,
);
