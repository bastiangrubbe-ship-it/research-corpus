import "@bastiangrubbe/ui-kit/style.css";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "./App";

const root = document.getElementById("root");
if (!root) throw new Error("no #root element found");

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>
);
