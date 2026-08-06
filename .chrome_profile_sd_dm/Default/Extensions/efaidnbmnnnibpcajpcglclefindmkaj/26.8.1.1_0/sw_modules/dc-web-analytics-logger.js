/*************************************************************************
* ADOBE CONFIDENTIAL
* ___________________
*
*  Copyright 2015 Adobe Systems Incorporated
*  All Rights Reserved.
*
* NOTICE:  All information contained herein is, and remains
* the property of Adobe Systems Incorporated and its suppliers,
* if any.  The intellectual and technical concepts contained
* herein are proprietary to Adobe Systems Incorporated and its
* suppliers and are protected by all applicable intellectual property laws,
* including trade secret and or copyright laws.
* Dissemination of this information or reproduction of this material
* is strictly forbidden unless prior written permission is obtained
* from Adobe Systems Incorporated.
**************************************************************************/
import{util as e}from"./util.js";import{common as t}from"./common.js";import{forceResetService as o}from"./force-reset-service.js";import{floodgate as s}from"./floodgate.js";import r from"./CacheStore.js";import{CACHE_PURGE_SCHEME as c}from"./constant.js";import{dcLocalStorage as a}from"../common/local-storage.js";import{OFFSCREEN_DOCUMENT_PATH as n}from"../common/constant.js";const i="dcWebAnalyticsConfig",m={allowedEvents:[],remove:!1};export const dcWebAnalyticsLogger=new class{constructor(){this.cacheStore=new r("dc-web-allowed-events"),this.promise=null}async fetchConfig(){try{const s=t.getDcWebAllowedEventsUrl();if(!s)return await this.cacheStore.get(i)||m;const r=async()=>{const t=await fetch(s);if(!t.ok)throw new Error(`Failed to fetch DCWeb analytics config from ${s}: ${t.statusText}`);const o=await t.json();return await this.cacheStore.set(i,o),o.remove&&await async function(){try{await e.hasOffscreenDocument()&&chrome.runtime.sendMessage({main_op:"removeDcWebIframe",target:"offscreen"})}catch{}}(),o},{executionResult:c}=await o.executeFeature("dc-web-allowed-events",r);if(c)return c}catch{}return await this.cacheStore.get(i)||m}async getConfig(){return this.promise||(this.promise=this.fetchConfig(),setTimeout(()=>{this.promise=null},9e5)),this.promise}async logToDcWeb(o,r={},i={}){try{if(!await s.hasFlag("dc-cv-ext-analytics-to-cdn",c.NO_CALL))return;const{allowedEvents:m}=await this.getConfig();if(!m.includes(o))return;const f=!0===a.getItem("ao"),l=`${n}?env=${t.getEnv()}`;await e.setupOffscreenDocument(l),chrome.runtime.sendMessage({main_op:"logToDcWeb",target:"offscreen",iframeURL:t.getDcWebAnalyticsUrl(),ao:f,pageName:o,eVars:r,props:i})}catch(e){}}};