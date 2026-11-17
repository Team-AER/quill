import { reportError } from './monitoring';
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./styles/app.css";
import { App } from "./App";

createRoot(document.getElementById("root")!, { onUncaughtError: reportError, onCaughtError: reportError }).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
