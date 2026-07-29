/* Where the backend lives.
 *
 * The dashboard is a static page; the API runs somewhere else. Put that
 * URL here -- this is the ONLY line to change when you deploy, and it is
 * a plain static file, so editing it needs no rebuild.
 *
 *   ""                                   same origin (local uvicorn)
 *   "https://dl-india-core-api.onrender.com"    a deployed backend
 *
 * ?api=https://... in the address bar overrides this for one visit, which
 * is handy for testing a new backend before committing to it.
 */
window.API_BASE =
  new URLSearchParams(location.search).get("api") ||
  "";
