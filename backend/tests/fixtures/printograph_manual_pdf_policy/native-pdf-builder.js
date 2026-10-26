// Executes saved public methods only, with no browser, network or source callback.
const fs=require('fs'),path=require('path');const dir=process.argv[2];
function method(name,file){const text=fs.readFileSync(path.join(dir,file),'utf8');return Function('s','o','document','return ({'+text+'}).'+name)(s,o,document)}
const selected={shape:15,size:727,orientation:1,paper:1,color:1,quantity:50,productionTime:1,proofType:100,manualProofType:1,fileDelivery:5,designServices:42,coating:5,paperFinish:1,turnaround:''};
const state={userSelectedOptions:selected,appliedPrices:{holeDrillingPrice:0},optionsHavingPrices:[],others:{blankProducts:[],apparelSizeStyle1:[],dynamicProducts:[],subItemProducts:[],defaultColoredProducts:[],defaultPaperedProducts:[]}};
const fields={39:{specs:['shape','size','orientation','paper','color'],options:['coating','paperFinish']}};
const s={getTotal:()=>0},o={A:{getPrintProductType:(_,type)=>type,isGeneric:()=>false}},document={};
s.getStatelessParam=method('getStatelessParam','get-stateless.js.txt');s.updateOptions=method('updateOptions','update-options.js.txt');
// The native stateless wrapper also handles artwork/cart metadata. That code is
// not invoked: only its proved getParameters+updateOptions identity projection
// reaches turnaroundParam, with no fee arithmetic, upload or ordering behavior.
s.parametersOnAddToCartStateless=(e,t,i,r)=>{const value=s.getStatelessParam(e,t,i,r);value.itemDetail.productOptions=[];return JSON.stringify(s.updateOptions(e,t,r,value))};
s.turnaroundParam=method('turnaroundParam','turnaround-param.js.txt');
const request=JSON.parse(s.turnaroundParam(state,39,{},fields,{},false,false));
process.stdout.write(JSON.stringify({request,source_options:request.productOptions,selected_paperFinish:selected.paperFinish,selected_manualProofType:selected.manualProofType}));
