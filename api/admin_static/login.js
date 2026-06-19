const byId = (id) => document.getElementById(id);

async function submitLogin(event) {
  event.preventDefault();
  const message = byId("loginMessage");
  message.textContent = "";

  const response = await fetch("/admin/api/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: byId("loginPassword").value }),
  });

  if (!response.ok) {
    message.textContent =
      response.status === 401 ? "Invalid password" : `${response.status} ${response.statusText}`;
    return;
  }

  window.location.href = "/admin";
}

byId("loginForm").addEventListener("submit", (event) => {
  submitLogin(event).catch((error) => {
    byId("loginMessage").textContent = error.message;
  });
});
