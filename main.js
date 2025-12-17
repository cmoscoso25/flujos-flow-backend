// Año automático
const y = document.getElementById('y');
if (y) y.textContent = new Date().getFullYear();

// Checkout placeholder
function openCheckout(){
  alert("Conecta aquí tu checkout real (Flow/Stripe). Si me pasas tu URL de pago, la dejo integrada.");
}
window.openCheckout = openCheckout;

// FAQ tipo acordeón (abre 1 y cierra el resto)
const faqs = document.querySelectorAll('#faq details');
faqs.forEach(d => {
  d.addEventListener('toggle', () => {
    if(d.open){
      faqs.forEach(other => { if(other !== d) other.open = false; });
    }
  });
});
